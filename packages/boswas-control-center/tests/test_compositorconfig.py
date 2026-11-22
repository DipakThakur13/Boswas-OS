"""compositorconfig: the schema, the layers, the writer of the user's files, the control socket, monitor layout.

Files live in a temporary directory; the control socket is a small server in
this process. The settings files are written through a stand-in for
userfiles.write_config (StandIn) and, in the Real* classes, through the real
one. RealCompositorTest starts the real compositor (headless, private
directories) and is opt-in:

    BOSWAS_TEST_COMPOSITOR=1 python3 -B -m unittest tests.test_compositorconfig
"""

from __future__ import annotations

import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures  # noqa: E402,F401  (import paths)

from boswas_control_center import compositorconfig as cc  # noqa: E402
from boswas_control_center import userfiles  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
COMPOSITOR = REPO / "packages" / "boswas-desktop" / "target" / "debug" / "boswas-compositor"
# What the headless backend of the real compositor answers to "outputs" (captured from it).
HEADLESS = [{"adaptive_sync": {"enabled": False, "supported": False},
             "description": "Boswas - Headless - HEADLESS-1", "enabled": True, "height": 1080, "make": "Boswas",
             "mode": {"height": 1080, "refresh": 60000, "width": 1920}, "model": "Headless",
             "modes": [{"height": 1080, "preferred": True, "refresh": 60000, "width": 1920}], "name": "HEADLESS-1",
             "physical_height_mm": 0, "physical_width_mm": 0, "rotation": "normal", "scale": 1.0,
             "usable": {"height": 1080, "width": 1920, "x": 0, "y": 0}, "width": 1920, "x": 0, "y": 0}]
PRESET_FILE = ("# Written by boswas-preset; changes made here are replaced by the next preset.\n"
               "[appearance]\nscheme = \"dark\"\n"
               "wallpaper = \"/usr/share/wallpapers/Boswas-Ocean/contents/images_dark/3840x2160.png\"\n")


class StandIn:
    """userfiles.write_config / remove_config for a temporary directory, recording what was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail: set[str] = set()

    def write(self, environ, name: str, data: bytes) -> str:
        self.calls.append(("write", name))
        if name in self.fail:
            raise userfiles.UserFileError(f"{name} could not be written (the disk is full).")
        assert isinstance(data, bytes) and name in cc_names()
        path = Path(cc.config_home(environ), name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def remove(self, environ, name: str) -> None:
        self.calls.append(("remove", name))
        assert name in cc_names()
        Path(cc.config_home(environ), name).unlink(missing_ok=True)


def cc_names() -> tuple[str, ...]:
    return cc.USER_NAME, cc.BACKUP_NAME, cc.APPEARANCE_NAME


class Files(unittest.TestCase):
    """A temporary home with the four layers; self.settings reads and writes them."""

    real_writer = False

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cc-config-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.environ = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config")}
        self.dir = self.home / ".config" / "boswas"
        self.user, self.look, self.bak = (self.dir / "compositor.toml", self.dir / "appearance.toml",
                                          self.dir / "compositor.toml.bak")
        self.defaults, self.system = self.tmp / "defaults.toml", self.tmp / "etc.toml"
        self.stand_in = StandIn()
        self.settings = self.make()

    def make(self, **keywords) -> cc.Settings:
        if not self.real_writer:
            keywords.setdefault("write", self.stand_in.write)
            keywords.setdefault("remove", self.stand_in.remove)
        keywords.setdefault("control", cc.Control({}))            # no compositor
        return cc.Settings(self.environ, system_files=(str(self.defaults), str(self.system)), **keywords)

    def put(self, path: Path, text: str | bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))

    def toml(self, path: Path) -> dict:
        return tomllib.loads(path.read_text(encoding="utf-8"))


class SchemaTest(unittest.TestCase):
    def test_values_are_normalised(self):
        self.assertEqual(cc.check_value("output", "scale", 2), 2.0)
        self.assertIsInstance(cc.check_value("output", "scale", 2), float)
        self.assertEqual(cc.check_value("pointer", "accel_speed", 0.33333), 0.333)
        self.assertEqual(str(cc.check_value("pointer", "accel_speed", -0.0001)), "0.0")
        self.assertEqual(cc.check_value("outputs.eDP-1", "mode", "2560x1440@59.951"), "2560x1440@59.951")
        self.assertEqual(cc.check_value("outputs.HDMI-A-1", "position", (1920, -200)), [1920, -200])
        self.assertEqual(cc.check_value("outputs.DP_2", "rotation", "270"), "270")
        self.assertEqual(cc.check_value("night_light", "from", "06:30"), "06:30")
        self.assertEqual(cc.check_value("keyboard", "layout", "us,de,in"), "us,de,in")
        self.assertEqual(cc.check_value("keyboard", "variant", ",neo,"), ",neo,")
        self.assertEqual(cc.check_value("keyboard", "options", "grp:alt_shift_toggle,caps:escape"),
                         "grp:alt_shift_toggle,caps:escape")
        self.assertEqual(cc.check_value("appearance", "wallpaper", "/home/a/Pictures/../Pictures/Sea ä.png"),
                         "/home/a/Pictures/Sea ä.png")
        self.assertEqual(cc.check_value("appearance", "wallpaper", ""), "")
        self.assertEqual(cc.check_value("power", "screen_off_seconds", 0), 0)
        self.assertEqual(cc.check_value("apps", "terminal", ("foot", "--title=#1")), ["foot", "--title=#1"])
        for scale in cc.SCALES:
            self.assertEqual(cc.check_value("outputs.eDP-1", "scale", scale), scale)

    def test_what_the_compositor_would_reject_is_refused(self):
        bad = [("output", "scale", 1.1), ("output", "scale", "1.5"), ("output", "scale", True),
               ("outputs.eDP-1", "scale", 3.0), ("night_light", "temperature", 2499),
               ("night_light", "temperature", 6501), ("night_light", "temperature", 4500.0),
               ("night_light", "from", "24:00"), ("night_light", "to", "6:30"), ("night_light", "to", "noon"),
               ("outputs.eDP-1", "mode", "big"), ("outputs.eDP-1", "mode", "1920x1080@0"),
               ("outputs.eDP-1", "mode", "99999x1"), ("outputs.eDP-1", "mode", "1920x1080@fast"),
               ("outputs.eDP-1", "rotation", "sideways"), ("outputs.eDP-1", "rotation", 90),
               ("outputs.eDP-1", "position", [0]), ("outputs.eDP-1", "position", [0, 40000]),
               ("outputs.eDP-1", "position", [0.5, 0]), ("outputs.eDP-1", "enabled", 1),
               ("outputs.bad name!", "scale", 1.0), ("outputs.", "scale", 1.0), ("outputs." + "x" * 65, "scale", 1.0),
               ("pointer", "accel_speed", 1.5), ("pointer", "accel_speed", float("nan")),
               ("pointer", "accel_speed", float("inf")), ("pointer", "accel_profile", "warp"),
               ("pointer", "cursor_size", 8), ("pointer", "cursor_size", 24.0), ("pointer", "left_handed", "yes"),
               ("keyboard", "repeat_delay", 99), ("keyboard", "repeat_delay", 2001),
               ("keyboard", "repeat_rate", 0), ("keyboard", "repeat_rate", 101), ("keyboard", "repeat_rate", True),
               ("keyboard", "layout", "us;rm -rf"), ("keyboard", "layout", "us de"), ("keyboard", "layout", "x\ny"),
               ("keyboard", "options", "grp:alt shift"), ("keyboard", "layout", "a" * 300),
               ("power", "screen_off_seconds", 5), ("power", "screen_off_seconds", 6 * 3600 + 1),
               ("power", "screen_off_seconds", -1), ("lock", "idle_seconds", -5),
               ("appearance", "scheme", "neon"), ("appearance", "wallpaper", "relative.png"),
               ("appearance", "wallpaper", "/tmp/a\nb.png"), ("appearance", "wallpaper", "/tmp/\udcff.png"),
               ("appearance", "wallpaper", "/" + "a" * 5000), ("apps", "terminal", "konsole"),
               ("apps", "terminal", ["a", 3])]
        for table, key, value in bad:
            with self.assertRaises(cc.InvalidValue, msg=(table, key, value)):
                cc.check_value(table, key, value)

    def test_the_administrators_keys_and_unknown_keys_are_refused(self):
        for table, key in (("policy", "lock_idle_max_seconds"), ("policy", "privileged_protocols"),
                           ("shell", "command"), ("lock", "command"), ("input_method", "command")):
            with self.assertRaises(cc.InvalidValue, msg=(table, key)) as caught:
                cc.check_value(table, key, ["/home/u/evil"])
            self.assertIn("administrator", str(caught.exception))
        for table, key in (("keyboard", "layuot"), ("nonsense", "x"), ("outputs.eDP-1", "primary"), ("outputs", "x")):
            with self.assertRaises(cc.InvalidValue, msg=(table, key)):
                cc.check_value(table, key, 1)

    def test_modes_and_clock_times_parse_like_the_compositor(self):
        self.assertEqual(cc.parse_mode("1920x1080"), (1920, 1080, None))
        self.assertEqual(cc.parse_mode("2560x1440@59.951"), (2560, 1440, 59951))
        for bad in ("", "1920", "1920x", "x1080", "0x0", "1920x1080@", "1920x1080@0", "1920x1080@fast", "99999x1",
                    "1920x1080@1001", " 1920x1080", "1920x1080@60 ", None, 5):
            self.assertIsNone(cc.parse_mode(bad), bad)
        self.assertEqual(cc.parse_clock("06:30"), 390)
        self.assertEqual(cc.parse_clock("23:59"), 1439)
        for bad in ("", "6:30", "24:00", "12:60", "noon", "12-30", "12:30 ", None):
            self.assertIsNone(cc.parse_clock(bad), bad)
        self.assertEqual([cc.clock_text(m) for m in (0, 390, 1439)], ["00:00", "06:30", "23:59"])

    def test_defaults_are_the_compositors(self):
        config = cc.defaults()
        self.assertEqual(config["keyboard"], {"layout": "us", "variant": "", "model": "", "options": "",
                                              "repeat_delay": 400, "repeat_rate": 30, "numlock": False})
        self.assertEqual(config["touchpad"], {"tap_to_click": True, "natural_scroll": True, "accel_speed": 0.0,
                                              "disable_while_typing": True, "gestures": True})
        self.assertEqual(config["night_light"], {"enabled": False, "temperature": 4500, "scheduled": False,
                                                 "from": "20:00", "to": "06:00"})
        self.assertEqual((config["power"]["screen_off_seconds"], config["lock"]["idle_seconds"],
                          config["policy"]["lock_idle_max_seconds"], config["pointer"]["cursor_size"],
                          config["output"]["scale"], config["appearance"]["scheme"]),
                         (300, 600, 600, 24, 1.0, "dark"))
        config["apps"]["terminal"].append("x")
        self.assertEqual(cc.defaults()["apps"]["terminal"], ["boswas-terminal"])
        self.assertEqual(cc.defaults()["apps"]["files"], ["boswas-files"])


class SnapshotTest(Files):
    def test_without_files_everything_is_built_in(self):
        snapshot = self.settings.snapshot()
        self.assertEqual(snapshot.values, cc.defaults())
        self.assertEqual(snapshot.source("lock", "idle_seconds"), "built-in")
        self.assertEqual(snapshot.problems(), [])
        self.assertEqual([layer.state for layer in snapshot.layers], ["missing"] * 4)
        self.assertEqual(snapshot.output("eDP-1"), {})
        self.assertIsNone(snapshot.get("outputs.eDP-1", "scale"))

    def test_layers_merge_key_by_key_and_name_their_source(self):
        self.put(self.defaults, "[keyboard]\nlayout = \"in\"\nrepeat_rate = 40\n[appearance]\nwallpaper = \"/a.png\"\n")
        self.put(self.system, "[keyboard]\nrepeat_delay = 500\n[output]\nscale = 2\n")
        self.put(self.user, "[keyboard]\nrepeat_rate = 20\n[output]\nscale = 1.5\n[appearance]\nscheme = \"dark\"\n")
        self.put(self.look, "[appearance]\nscheme = \"light\"\n")
        snapshot = self.settings.snapshot()
        self.assertEqual([snapshot.get("keyboard", key) for key in ("layout", "repeat_rate", "repeat_delay")],
                         ["in", 20, 500])
        self.assertEqual([snapshot.source("keyboard", key) for key in ("layout", "repeat_rate", "repeat_delay",
                                                                         "numlock")],
                         ["defaults", "user", "system", "built-in"])
        self.assertEqual((snapshot.get("output", "scale"), snapshot.source("output", "scale")), (1.5, "user"))
        self.assertEqual((snapshot.get("appearance", "scheme"), snapshot.source("appearance", "scheme")),
                         ("light", "appearance"))
        self.assertEqual(snapshot.get("appearance", "wallpaper"), "/a.png")
        self.assertEqual(self.settings.effective("keyboard", "repeat_rate"), 20)

    def test_a_user_cannot_relax_the_lock_policy_or_name_trusted_programs(self):
        self.put(self.user, "[lock]\nidle_seconds = 0\ncommand = [\"/home/u/evil\"]\n"
                            "[policy]\nlock_idle_max_seconds = 0\nprivileged_protocols = \"all\"\nnonsense = 1\n"
                            "[shell]\ncommand = [\"/home/u/evil\"]\n[input_method]\ncommand = [\"/home/u/keys\"]\n")
        snapshot = self.settings.snapshot()
        self.assertEqual(snapshot.layer("user").state, "used")
        self.assertEqual(snapshot.get("lock", "idle_seconds"), 600)
        self.assertEqual(snapshot.lock_limit, 600)
        self.assertEqual(snapshot.get("policy", "privileged_protocols"), "shell")
        self.assertEqual(snapshot.get("lock", "command"), ["/usr/libexec/boswas/boswas-shell", "--lock"])
        self.assertEqual(snapshot.get("shell", "command"), ["/usr/libexec/boswas/boswas-shell"])
        self.assertEqual(snapshot.get("input_method", "command"), [])
        self.assertEqual([snapshot.lock_allowed(s) for s in (0, 60, 600, 601, -1)], [False, True, True, False, False])
        self.put(self.user, "[lock]\nidle_seconds = 120\n")
        self.assertEqual(self.settings.snapshot().get("lock", "idle_seconds"), 120)

    def test_an_administrator_may_change_the_policy(self):
        self.put(self.system, "[policy]\nlock_idle_max_seconds = 0\n[lock]\nidle_seconds = 0\n")
        snapshot = self.settings.snapshot()
        self.assertEqual((snapshot.get("lock", "idle_seconds"), snapshot.lock_limit), (0, 0))
        self.assertEqual(snapshot.source("policy", "lock_idle_max_seconds"), "system")
        self.assertTrue(snapshot.lock_allowed(0) and snapshot.lock_allowed(86400))
        self.put(self.system, "[policy]\nlock_idle_max_seconds = 300\n")
        self.put(self.user, "[lock]\nidle_seconds = 1800\n")
        self.assertEqual(self.settings.snapshot().get("lock", "idle_seconds"), 300)

    def test_values_are_brought_into_range_like_the_compositor_does(self):
        self.put(self.user, "[keyboard]\nrepeat_delay = 1\nrepeat_rate = 100000\n"
                            "[pointer]\naccel_speed = 9.0\ncursor_size = 1\naccel_profile = \"warp\"\n"
                            "[touchpad]\naccel_speed = -9\n[output]\nscale = 3.3\n[appearance]\nscheme = \"neon\"\n"
                            "[night_light]\ntemperature = 100\nfrom = \"late\"\n[power]\nscreen_off_seconds = 5\n")
        get = self.settings.snapshot().get
        self.assertEqual([get("keyboard", "repeat_delay"), get("keyboard", "repeat_rate"),
                          get("pointer", "accel_speed"), get("pointer", "cursor_size"),
                          get("pointer", "accel_profile"), get("touchpad", "accel_speed"),
                          get("output", "scale"), get("appearance", "scheme"), get("night_light", "temperature"),
                          get("night_light", "from"), get("night_light", "to"), get("power", "screen_off_seconds")],
                         [100, 100, 1.0, 16, "adaptive", -1.0, 1.0, "dark", 2500, "20:00", "06:00", 30])
        self.put(self.user, "[pointer]\naccel_speed = nan\n[power]\nscreen_off_seconds = 0\n")
        get = self.settings.snapshot().get
        self.assertEqual((get("pointer", "accel_speed"), get("power", "screen_off_seconds")), (0.0, 0))

    def test_output_settings_are_validated_one_by_one(self):
        self.put(self.user, "[outputs.eDP-1]\nscale = 1.5\nmode = \"1920x1080@60\"\nposition = [0, 0]\n"
                            "rotation = \"90\"\n[outputs.HDMI-A-1]\nenabled = false\nscale = 3.0\nmode = \"big\"\n"
                            "rotation = \"sideways\"\nposition = [99999, 0]\n[outputs.\"bad name!\"]\nscale = 1.0\n")
        snapshot = self.settings.snapshot()
        self.assertEqual(snapshot.output("eDP-1"), {"scale": 1.5, "mode": "1920x1080@60", "position": [0, 0],
                                                    "rotation": "90"})
        self.assertEqual(snapshot.output("HDMI-A-1"), {"enabled": False})
        self.assertNotIn("bad name!", snapshot.values["outputs"])
        self.assertEqual(snapshot.source("outputs.eDP-1", "scale"), "user")
        self.assertEqual(snapshot.source("outputs.eDP-1", "enabled"), "built-in")

    def test_variable_refresh_rate_layers_like_the_other_monitor_keys(self):
        self.put(self.system, "[outputs.DP-1]\nadaptive_sync = true\n[outputs.DP-2]\nadaptive_sync = true\n")
        self.put(self.user, "[outputs.DP-1]\nscale = 1.25\n[outputs.DP-2]\nadaptive_sync = false\n")
        snapshot = self.settings.snapshot()
        self.assertEqual(snapshot.output("DP-1"), {"adaptive_sync": True, "scale": 1.25})
        self.assertEqual(snapshot.source("outputs.DP-1", "adaptive_sync"), "system")
        self.assertEqual((snapshot.get("outputs.DP-2", "adaptive_sync"), snapshot.source("outputs.DP-2", "adaptive_sync")),
                         (False, "user"))
        self.assertIsNone(snapshot.get("outputs.DP-3", "adaptive_sync"), "not set: the compositor keeps it off")
        # A value of the wrong kind makes the compositor ignore the file, and Settings says so.
        self.put(self.user, "[outputs.DP-2]\nadaptive_sync = \"yes\"\nscale = 2.0\n")
        snapshot = self.settings.snapshot()
        self.assertEqual(snapshot.output("DP-2"), {"adaptive_sync": True})
        self.assertTrue(snapshot.problems())
        self.settings.update({("outputs.DP-2", "adaptive_sync"): False})
        self.assertEqual(self.toml(self.user)["outputs"]["DP-2"]["adaptive_sync"], False)

    def test_the_appearance_file_only_changes_the_look(self):
        self.put(self.user, "[appearance]\nscheme = \"dark\"\n[keyboard]\nlayout = \"in\"\n")
        self.put(self.look, "[appearance]\nscheme = \"light\"\n[lock]\nidle_seconds = 0\n")
        snapshot = self.settings.snapshot()
        self.assertEqual(snapshot.layer("appearance").state, "skipped")
        self.assertEqual((snapshot.get("appearance", "scheme"), snapshot.get("keyboard", "layout")), ("dark", "in"))
        self.assertIn("only contain the [appearance] table", snapshot.problems()[0])
        self.put(self.look, "appearance = 3\n")
        self.assertEqual(self.settings.snapshot().layer("appearance").state, "skipped")

    def test_an_unknown_key_or_a_wrong_type_makes_the_desktop_ignore_that_file(self):
        self.put(self.system, "[keyboard]\nlayout = \"de\"\n")
        for text in ("[keyboard]\nlayuot = \"us\"\n", "[nonsense]\nx = 1\n", "[keyboard]\nrepeat_delay = 250.5\n",
                     "[keyboard]\nrepeat_delay = \"fast\"\n", "keyboard = 3\n", "[outputs]\neDP-1 = 3\n",
                     "[outputs.eDP-1]\nprimary = true\n", "[outputs.eDP-1]\nposition = [1, 2, 3]\n",
                     "[pointer]\ncursor_size = -1\n", "[keyboard]\nrepeat_rate = 99999999999999999999\n",
                     "[apps]\nterminal = \"konsole\"\n", "[night_light]\ntemperature = 4500.5\n", "outputs = 1\n"):
            self.put(self.user, text)
            snapshot = self.settings.snapshot()
            self.assertEqual(snapshot.layer("user").state, "rejected", text)
            self.assertEqual(snapshot.get("keyboard", "layout"), "de", text)       # the other files stay in use
            self.assertEqual(snapshot.source("keyboard", "layout"), "system", text)
            self.assertIn("ignores this file", snapshot.problems()[0])
        self.put(self.user, "[output]\nscale = 2\n[pointer]\naccel_speed = 1\n")       # whole numbers are fine
        snapshot = self.settings.snapshot()
        self.assertEqual((snapshot.layer("user").state, snapshot.get("output", "scale"),
                          snapshot.get("keyboard", "layout")), ("used", 2.0, "de"))

    def test_a_file_that_is_not_toml_is_skipped_alone(self):
        self.put(self.system, "[keyboard]\nlayout = \"de\"\n")
        big = "# x\n" * (cc.MAX_BYTES // 4 + 1)
        for content in ("this is not = = toml", b"\xff\xfe\x00[keyboard]", big, "x = [" * 2000):
            self.put(self.user, content)
            snapshot = self.settings.snapshot()
            self.assertEqual(snapshot.layer("user").state, "skipped", str(content)[:30])
            self.assertEqual(snapshot.get("keyboard", "layout"), "de")
            self.assertEqual(len(snapshot.problems()), 1)
        self.user.unlink()
        self.user.mkdir()
        self.assertEqual(self.settings.snapshot().layer("user").state, "skipped")

    def test_no_home_means_no_user_layers(self):
        settings = cc.Settings({}, system_files=(str(self.defaults), str(self.system)))
        self.assertEqual(settings.snapshot().values, cc.defaults())
        self.assertIsNone(settings.directory())
        self.assertIsNone(cc.config_home({"HOME": "relative", "XDG_CONFIG_HOME": "also/relative"}))
        self.assertEqual(cc.config_home({"HOME": "/home/a", "XDG_CONFIG_HOME": "relative"}), "/home/a/.config")
        self.assertEqual(cc.config_home({"HOME": "/home/a", "XDG_CONFIG_HOME": "/x/y/"}), "/x/y")


class WriterTest(unittest.TestCase):
    def test_every_user_key_round_trips_and_nothing_else_is_written(self):
        config = {table: {key: (found.default if found.default is not None else "")
                          for key, found in keys.items()} for table, keys in cc.SCHEMA.items()}
        config["outputs"] = {"eDP-1": {"enabled": True, "mode": "1920x1080@60", "scale": 1.25, "position": [0, -10],
                                       "rotation": "90", "adaptive_sync": True}, "HDMI-A-1": {"enabled": False},
                             "bad name": {"scale": 1.0}, "DP-3": {}}
        config["appearance"]["wallpaper"] = "/home/a/My \"best\" \\ picture ä世.png"
        text = cc.serialize(config)
        parsed = tomllib.loads(text)
        self.assertTrue(text.startswith(cc.HEADER))
        self.assertEqual(sorted(parsed), ["appearance", "apps", "keyboard", "lock", "night_light", "output", "outputs",
                                          "pointer", "power", "touchpad", "xwayland"])
        self.assertEqual(parsed["lock"], {"idle_seconds": 600})           # never the command
        self.assertEqual(parsed["outputs"], {"eDP-1": config["outputs"]["eDP-1"], "HDMI-A-1": {"enabled": False}})
        for table in ("keyboard", "pointer", "touchpad", "night_light", "apps", "appearance"):
            self.assertEqual(parsed[table], config[table], table)
        self.assertIsInstance(parsed["output"]["scale"], float)
        self.assertIsInstance(parsed["keyboard"]["repeat_delay"], int)
        self.assertNotIn("policy", text)
        self.assertNotIn("input_method", text)
        self.assertEqual(cc._schema_problem(parsed), "")
        self.assertEqual(cc.serialize({}), cc.HEADER)
        self.assertEqual(cc.serialize({"keyboard": {}}, ""), "")

    def test_strings_and_numbers(self):
        for text in ("", "plain", 'quote " and \\ backslash', "tab\tnewline\ncr\r", "bell\x07 del\x7f nul\x00",
                     "äöü 世界 \U0001f600", "# not a comment", "'single'"):
            self.assertEqual(tomllib.loads("x = " + cc.toml_string(text))["x"], text)
        for value, written in ((1.0, "1.0"), (1.25, "1.25"), (-0.3, "-0.3"), (0.333, "0.333"), (2, "2.0"),
                               (0.00001, "0.0")):
            self.assertEqual(cc.toml_value("float", value), written)
            self.assertIsInstance(tomllib.loads("x = " + written)["x"], float)
        self.assertEqual(cc.toml_value("position", [-5, 7]), "[-5, 7]")
        self.assertEqual(cc.toml_value("bool", False), "false")

    def test_comments_are_found_outside_strings_only(self):
        for text in ("# a comment\n[keyboard]\n", "[keyboard] # here\nlayout = \"us\"\n", "x = 1 # trailing",
                     "a = '''\n# inside\n''' # outside\n", 'a = """x""""" # after two extra quotes'):
            tomllib.loads(text)
            self.assertTrue(cc.has_comments(text), text)
        for text in ("", "[keyboard]\nlayout = \"us\"\n", "a = \"# not one\"\n", "a = '# not one'\n",
                     'a = """\n# inside a multi-line string\n"""\n', "a = '''\n# inside\n'''\n",
                     'a = "quote \\" # still inside"\n', 'a = """two quotes "" # inside"""\n',
                     'a = ["x # y", \'z # w\']\n', 'a = """x"""""\n'):
            tomllib.loads(text)
            self.assertFalse(cc.has_comments(text), text)

    def test_carry_over_keeps_documented_values_and_names_the_rest(self):
        kept, dropped = cc.carry_over(tomllib.loads(
            "[keyboard]\nlayout = \"us,de\"\nlayuot = 1\nrepeat_delay = 99\n[apps]\nterminal = [\"foot\"]\n"
            "[policy]\nlock_idle_max_seconds = 0\n[lock]\nidle_seconds = 300\ncommand = [\"x\"]\n[nonsense]\na = 1\n"
            "[outputs.eDP-1]\nscale = 2\nrotation = \"sideways\"\n[outputs.\"bad name\"]\nscale = 1.0\n"
            "[output]\nscale = 1.5\n[xwayland]\nenabled = false\n[shell]\ncommand = [\"x\"]\n"))
        self.assertEqual(kept, {"keyboard": {"layout": "us,de"}, "apps": {"terminal": ["foot"]},
                                "lock": {"idle_seconds": 300}, "outputs": {"eDP-1": {"scale": 2.0}},
                                "output": {"scale": 1.5}, "xwayland": {"enabled": False}})
        self.assertEqual(dropped, ["keyboard.layuot", "keyboard.repeat_delay",
                                   "[policy] (only the administrator's files may set it)",
                                   "lock.command (only the administrator's files may set it)", "[nonsense]",
                                   "outputs.eDP-1.rotation", "outputs.bad name",
                                   "[shell] (only the administrator's files may set it)"])


class UpdateTest(Files):
    def test_a_first_change_creates_the_file(self):
        saved = self.settings.update({("touchpad", "tap_to_click"): False, ("pointer", "accel_speed"): 0.4,
                                      ("outputs.eDP-1", "scale"): 1.5})
        self.assertEqual(saved, cc.Saved((str(self.user),)))
        self.assertTrue(self.user.read_text().startswith(cc.HEADER))
        self.assertEqual(self.toml(self.user), {"touchpad": {"tap_to_click": False}, "pointer": {"accel_speed": 0.4},
                                                "outputs": {"eDP-1": {"scale": 1.5}}})
        snapshot = self.settings.snapshot()
        self.assertEqual((snapshot.layer("user").state, snapshot.get("touchpad", "tap_to_click"),
                          snapshot.get("outputs.eDP-1", "scale")), ("used", False, 1.5))
        self.assertFalse(self.bak.exists())

    def test_other_documented_keys_and_tables_are_kept(self):
        self.settings.update({("keyboard", "layout"): "us,de", ("keyboard", "variant"): ",neo",
                              ("apps", "terminal"): ["foot"], ("xwayland", "enabled"): False,
                              ("outputs.DP-1", "position"): [1920, 0], ("outputs.DP-1", "mode"): "2560x1440@144",
                              ("night_light", "enabled"): True, ("lock", "idle_seconds"): 300})
        before = self.toml(self.user)
        saved = self.settings.update({("keyboard", "repeat_rate"): 25, ("outputs.DP-1", "scale"): 1.25})
        self.assertEqual((saved.backup, saved.dropped, saved.notice), ("", (), ""))
        after = self.toml(self.user)
        before["keyboard"]["repeat_rate"] = 25
        before["outputs"]["DP-1"]["scale"] = 1.25
        self.assertEqual(after, before)

    def test_none_removes_a_setting_and_an_empty_table(self):
        self.settings.update({("power", "screen_off_seconds"): 900, ("keyboard", "numlock"): True,
                              ("outputs.DP-1", "scale"): 2.0, ("outputs.DP-1", "rotation"): "90"})
        self.settings.update({("power", "screen_off_seconds"): None, ("outputs.DP-1", "scale"): None,
                              ("touchpad", "gestures"): None})
        self.assertEqual(self.toml(self.user), {"keyboard": {"numlock": True}, "outputs": {"DP-1": {"rotation": "90"}}})
        self.settings.update({("outputs.DP-1", "rotation"): None, ("keyboard", "numlock"): None})
        self.assertEqual(self.user.read_text(), cc.HEADER)
        self.assertEqual(self.settings.snapshot().layer("user").state, "used")

    def test_a_file_with_comments_is_backed_up_once(self):
        original = "# my own notes\n[keyboard]\nlayout = \"de\"   # German\n\n[apps]\nterminal = [\"foot\"]\n"
        self.put(self.user, original)
        saved = self.settings.update({("keyboard", "numlock"): True})
        self.assertEqual(saved.backup, str(self.bak))
        self.assertEqual(saved.dropped, ("comments",))
        self.assertIn("comments", saved.notice)
        self.assertIn(str(self.bak), saved.notice)
        self.assertEqual(self.bak.read_text(), original)
        self.assertEqual(self.toml(self.user), {"keyboard": {"layout": "de", "numlock": True},
                                                "apps": {"terminal": ["foot"]}})
        if not self.real_writer:
            self.assertEqual(self.stand_in.calls, [("write", cc.BACKUP_NAME), ("write", cc.USER_NAME)])
        again = self.settings.update({("keyboard", "numlock"): False})
        self.assertEqual((again.backup, again.notice), ("", ""))
        self.assertEqual(self.bak.read_text(), original)

    def test_unknown_keys_are_not_dropped_silently(self):
        original = ("[keyboard]\nlayuot = \"us\"\nlayout = \"fr\"\n[nonsense]\nx = 1\n[policy]\n"
                    "lock_idle_max_seconds = 0\n[lock]\ncommand = [\"/home/u/evil\"]\nidle_seconds = 120\n"
                    "[pointer]\naccel_speed = 5\n")
        self.put(self.user, original)
        self.assertEqual(self.settings.snapshot().layer("user").state, "rejected")
        saved = self.settings.update({("touchpad", "gestures"): False})
        self.assertEqual(self.bak.read_text(), original)
        self.assertEqual(set(saved.dropped), {"keyboard.layuot", "[nonsense]", "pointer.accel_speed",
                                              "[policy] (only the administrator's files may set it)",
                                              "lock.command (only the administrator's files may set it)"})
        for name in ("keyboard.layuot", "[nonsense]"):
            self.assertIn(name, saved.notice)
        self.assertEqual(self.toml(self.user), {"keyboard": {"layout": "fr"}, "lock": {"idle_seconds": 120},
                                                "touchpad": {"gestures": False}})
        snapshot = self.settings.snapshot()
        self.assertEqual((snapshot.layer("user").state, snapshot.problems()), ("used", []))

    def test_a_file_that_is_not_toml_is_replaced_with_a_backup(self):
        for original in (b"this is not = = toml\n", b"\xff\xfe binary"):
            self.put(self.user, original)
            saved = self.settings.update({("keyboard", "repeat_delay"): 300})
            self.assertEqual(self.bak.read_bytes(), original)
            self.assertEqual(saved.dropped, ("everything",))
            self.assertIn("could not be read as a settings file", saved.notice)
            self.assertEqual(self.toml(self.user), {"keyboard": {"repeat_delay": 300}})

    def test_setting_what_the_file_already_says_leaves_it_alone(self):
        original = "# mine\n[keyboard]\nrepeat_delay = 300\n"
        self.put(self.user, original)
        saved = self.settings.update({("keyboard", "repeat_delay"): 300, ("touchpad", "gestures"): None})
        self.assertEqual(saved, cc.Saved())
        self.assertEqual(self.user.read_text(), original)
        self.assertFalse(self.bak.exists())
        if not self.real_writer:
            self.assertEqual(self.stand_in.calls, [])

    def test_a_rejected_file_is_repaired_even_by_a_value_it_already_holds(self):
        self.put(self.user, "[touchpad]\ntap_to_click = false\nnonsense = 1\n")
        self.assertTrue(self.settings.snapshot().get("touchpad", "tap_to_click"))      # the file is not in use
        saved = self.settings.update({("touchpad", "tap_to_click"): False})
        self.assertEqual(saved.dropped, ("touchpad.nonsense",))
        self.assertFalse(self.settings.snapshot().get("touchpad", "tap_to_click"))

    def test_an_invalid_value_writes_nothing(self):
        self.put(self.user, "[keyboard]\nlayout = \"de\"\n")
        for changes in ({("output", "scale"): 1.1}, {("keyboard", "numlock"): True, ("night_light", "from"): "25:00"},
                        {("policy", "lock_idle_max_seconds"): 0}, {("lock", "command"): None},
                        {("shell", "command"): ["/home/u/evil"]}, {("nonsense", "x"): 1},
                        {("outputs.bad name", "scale"): 1.0}):
            with self.assertRaises(cc.InvalidValue, msg=changes):
                self.settings.update(changes)
        self.assertEqual(self.user.read_text(), "[keyboard]\nlayout = \"de\"\n")
        self.assertFalse(self.bak.exists())

    def test_the_lock_time_respects_the_policy(self):
        for seconds in (0, 601, 1800):
            with self.assertRaises(cc.InvalidValue, msg=seconds) as caught:
                self.settings.update({("lock", "idle_seconds"): seconds})
            self.assertIn("organisation", str(caught.exception))
            self.assertIn("10 minutes", str(caught.exception))
        self.assertFalse(self.user.exists())
        self.settings.update({("lock", "idle_seconds"): 600})
        self.settings.update({("lock", "idle_seconds"): 60})
        self.assertEqual(self.settings.snapshot().get("lock", "idle_seconds"), 60)
        self.put(self.system, "[policy]\nlock_idle_max_seconds = 0\n")
        self.settings.update({("lock", "idle_seconds"): 0})
        self.assertEqual(self.settings.snapshot().get("lock", "idle_seconds"), 0)

    def test_a_file_that_cannot_be_copied_is_not_replaced(self):
        self.put(self.user, "# x\n" * (cc.MAX_BYTES // 4 + 1))
        size = self.user.stat().st_size
        with self.assertRaises(cc.SaveError) as caught:
            self.settings.update({("keyboard", "numlock"): True})
        self.assertIn("in the way", str(caught.exception))
        self.assertEqual(self.user.stat().st_size, size)
        self.assertFalse(self.bak.exists())
        self.user.unlink()
        self.user.mkdir()
        with self.assertRaises(cc.SaveError):
            self.settings.update({("keyboard", "numlock"): True})
        self.assertTrue(self.user.is_dir())

    def test_no_home_cannot_save(self):
        settings = cc.Settings({}, system_files=(str(self.defaults), str(self.system)),
                               write=self.stand_in.write, remove=self.stand_in.remove)
        with self.assertRaises(cc.SaveError) as caught:
            settings.update({("keyboard", "numlock"): True})
        self.assertIn("home folder", str(caught.exception))

    def test_restore_puts_the_exact_bytes_back(self):
        self.assertIsNone(self.settings.user_file())
        self.settings.update({("outputs.eDP-1", "scale"): 2.0})
        self.settings.restore(None)
        self.assertFalse(self.user.exists())
        original = b"# hand written\r\n[outputs.eDP-1]\r\nscale = 1.0  # exact\r\n"
        self.put(self.user, original)
        before = self.settings.user_file()
        self.settings.update({("outputs.eDP-1", "scale"): 2.0, ("outputs.eDP-1", "rotation"): "90"})
        self.assertNotEqual(self.user.read_bytes(), original)
        self.settings.restore(before)
        self.assertEqual(self.user.read_bytes(), original)

    def test_changes_from_several_threads_do_not_lose_each_other(self):
        keys = [("keyboard", "numlock", True), ("touchpad", "gestures", False), ("pointer", "left_handed", True),
                ("night_light", "enabled", True), ("xwayland", "enabled", False), ("power", "screen_off_seconds", 600)]
        threads = [threading.Thread(target=self.settings.update, args=({(table, key): value},))
                   for table, key, value in keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        document = self.toml(self.user)
        for table, key, value in keys:
            self.assertEqual(document[table][key], value)


class AppearanceTest(Files):
    def test_the_scheme_keeps_the_wallpaper_the_preset_recorded(self):
        self.put(self.look, PRESET_FILE)
        saved = self.settings.update({("appearance", "scheme"): "light"})
        self.assertEqual(saved.written, (str(self.look),))
        self.assertEqual(self.toml(self.look), {"appearance": {
            "scheme": "light", "wallpaper": "/usr/share/wallpapers/Boswas-Ocean/contents/images_dark/3840x2160.png"}})
        self.assertFalse(self.user.exists())
        self.settings.update({("appearance", "wallpaper"): "/home/a/Pictures/sea.jpg"})
        self.assertEqual(self.toml(self.look), {"appearance": {"scheme": "light",
                                                               "wallpaper": "/home/a/Pictures/sea.jpg"}})
        snapshot = self.settings.snapshot()
        self.assertEqual((snapshot.layer("appearance").state, snapshot.source("appearance", "wallpaper")),
                         ("used", "appearance"))

    def test_the_file_stays_readable_for_the_preset_tool_and_the_toolkit(self):
        sys.path.insert(0, str(REPO / "desktop" / "presets"))
        from boswas_fluent.appearance import read_appearance
        from boswas_preset import cli
        preset = {"id": "pearl", "label": "Pearl", "variant": "light"}
        cli.write_appearance(self.environ, preset)                       # what "boswas-preset apply pearl" writes
        self.assertEqual(self.settings.snapshot().get("appearance", "wallpaper"), cli.wallpaper_image(preset))
        self.settings.update({("appearance", "scheme"): "dark", ("appearance", "reduced_motion"): True})
        self.assertEqual(self.toml(self.look)["appearance"], {"scheme": "dark",
                                                              "wallpaper": cli.wallpaper_image(preset)})
        self.assertEqual(read_appearance(system_files=(), user=self.dir).scheme, "dark")
        self.assertTrue(read_appearance(system_files=(), user=self.dir).reduced_motion)
        cli.write_appearance(self.environ, preset)                       # the next preset replaces the file ...
        look = read_appearance(system_files=(), user=self.dir)
        self.assertEqual((look.scheme, look.reduced_motion), ("light", True))     # ... and motion stays as chosen

    def test_reduced_motion_lives_in_the_user_file(self):
        self.put(self.look, PRESET_FILE)
        saved = self.settings.update({("appearance", "reduced_motion"): True})
        self.assertEqual(saved.written, (str(self.user),))
        self.assertEqual(self.look.read_text(), PRESET_FILE)             # untouched, byte for byte
        self.assertEqual(self.toml(self.user), {"appearance": {"reduced_motion": True}})
        self.assertTrue(self.settings.snapshot().get("appearance", "reduced_motion"))
        # A copy in appearance.toml would win over the user's file: it is taken out.
        self.put(self.look, "[appearance]\nscheme = \"light\"\nreduced_motion = true\n")
        self.settings.update({("appearance", "reduced_motion"): False})
        self.assertEqual(self.toml(self.look), {"appearance": {"scheme": "light"}})
        self.assertFalse(self.settings.snapshot().get("appearance", "reduced_motion"))

    def test_the_default_background_clears_the_key_everywhere(self):
        self.put(self.look, PRESET_FILE)
        self.put(self.user, "[appearance]\nwallpaper = \"/old.png\"\n[keyboard]\nnumlock = true\n")
        self.settings.update({("appearance", "wallpaper"): None})
        self.assertEqual(self.toml(self.look), {"appearance": {"scheme": "dark"}})
        self.assertEqual(self.toml(self.user), {"keyboard": {"numlock": True}})
        self.assertEqual(self.settings.snapshot().get("appearance", "wallpaper"), "")
        self.settings.update({("appearance", "scheme"): None})
        self.assertFalse(self.look.exists())                             # no empty file is left behind
        self.assertEqual(self.settings.update({("appearance", "wallpaper"): None}), cc.Saved())

    def test_a_broken_appearance_file_is_cleaned_and_said_so(self):
        self.put(self.look, "[appearance]\nscheme = \"light\"\naccent = \"gold\"\n[lock]\nidle_seconds = 0\n")
        saved = self.settings.update({("appearance", "wallpaper"): "/a.png"})
        self.assertEqual(self.toml(self.look), {"appearance": {"scheme": "light", "wallpaper": "/a.png"}})
        self.assertEqual(set(saved.dropped), {"appearance.accent", "[lock]"})
        self.assertIn("does not belong there", saved.notice)
        self.put(self.look, "not toml = = =")
        self.settings.update({("appearance", "scheme"): "dark"})
        self.assertEqual(self.toml(self.look), {"appearance": {"scheme": "dark"}})


class FailingWriterTest(Files):
    def test_a_failed_backup_changes_nothing(self):
        original = "# notes\n[keyboard]\nlayout = \"de\"\n"
        self.put(self.user, original)
        self.stand_in.fail = {cc.BACKUP_NAME}
        with self.assertRaises(cc.SaveError) as caught:
            self.settings.update({("keyboard", "numlock"): True})
        self.assertIn("disk is full", str(caught.exception))
        self.assertEqual(self.user.read_text(), original)
        self.assertEqual(self.stand_in.calls, [("write", cc.BACKUP_NAME)])

    def test_a_failed_write_is_a_save_error(self):
        self.stand_in.fail = {cc.USER_NAME}
        with self.assertRaises(cc.SaveError):
            self.settings.update({("keyboard", "numlock"): True})
        with self.assertRaises(cc.SaveError):
            self.settings.restore(b"x = 1\n")

    def test_without_a_writer_nothing_can_be_saved(self):
        settings = cc.Settings(self.environ, system_files=(str(self.defaults), str(self.system)))
        original = (userfiles.write_config, userfiles.remove_config)
        try:
            del userfiles.write_config, userfiles.remove_config
            with self.assertRaises(cc.SaveError):
                settings.update({("keyboard", "numlock"): True})
            with self.assertRaises(cc.SaveError):
                settings.restore(None)
        finally:
            userfiles.write_config, userfiles.remove_config = original


class RealUpdateTest(UpdateTest):
    """The same through userfiles.write_config and remove_config."""

    real_writer = True

    def test_files_are_private(self):
        self.settings.update({("keyboard", "numlock"): True})
        self.assertEqual(self.user.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)

    def test_a_symbolic_link_is_not_written_through(self):
        target = self.tmp / "elsewhere.toml"
        target.write_text("[keyboard]\nlayout = \"de\"\n")
        self.dir.mkdir(parents=True)
        self.user.symlink_to(target)
        with self.assertRaises(cc.SaveError) as caught:
            self.settings.update({("keyboard", "numlock"): True})
        self.assertIn("symbolic link", str(caught.exception))
        self.assertEqual(target.read_text(), "[keyboard]\nlayout = \"de\"\n")


class RealAppearanceTest(AppearanceTest):
    real_writer = True


# --- the control socket ---------------------------------------------------------------------------------------

class FakeCompositor:
    """A control socket that answers each connection with what ``answer`` returns for the request."""

    def __init__(self, answer) -> None:
        self.directory = tempfile.mkdtemp(prefix="bwT.", dir="/tmp")
        self.path = os.path.join(self.directory, "boswas-wayland-9.sock")
        self.environ = {"XDG_RUNTIME_DIR": self.directory, "WAYLAND_DISPLAY": "wayland-9"}
        self.answer = answer
        self.requests: list[dict] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(8)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(2)
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    request = json.loads(data)
                    self.requests.append(request)
                    reply = self.answer(request)
                    if reply is not None:
                        connection.sendall(reply if isinstance(reply, bytes) else (json.dumps(reply) + "\n").encode())
                    else:
                        threading.Event().wait(1.0)
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        self.server.close()
        shutil.rmtree(self.directory, ignore_errors=True)


class ControlTest(unittest.TestCase):
    def serve(self, answer) -> FakeCompositor:
        fake = FakeCompositor(answer)
        self.addCleanup(fake.close)
        return fake

    def test_reload_and_outputs(self):
        def answer(request):
            if request["cmd"] == "reload-config":
                return {"id": None, "ok": True, "data": {"warnings": ["lock.idle_seconds limited to 600 by policy",
                                                                      "configuration ignored: unknown field\nin x\n",
                                                                      7]}}
            return {"ok": True, "data": HEADLESS}
        fake = self.serve(answer)
        control = cc.Control(fake.environ)
        self.assertEqual(control.socket_path(), fake.path)
        self.assertEqual(control.reload_config(), ["lock.idle_seconds limited to 600 by policy",
                                                   "configuration ignored: unknown field in x"])
        outputs = control.outputs()
        self.assertEqual(fake.requests, [{"cmd": "reload-config"}, {"cmd": "outputs"}])
        self.assertEqual(len(outputs), 1)
        output = outputs[0]
        self.assertEqual((output.name, output.enabled, output.label, output.x, output.y, output.width, output.height,
                          output.scale, output.rotation), ("HEADLESS-1", True, "Boswas Headless", 0, 0, 1920, 1080,
                                                           1.0, "normal"))
        self.assertEqual(output.mode, cc.Mode(1920, 1080, 60000, True))
        self.assertEqual(output.modes, (cc.Mode(1920, 1080, 60000, True),))

    def test_failures_are_told_apart(self):
        replies = {"refused": {"ok": False, "error": "\"nonsense\" is not available on this connection"},
                   "garbage": b"not json\n", "list": b"[1, 2]\n", "closed": b"", "silent": None,
                   "huge": b"x" * (cc.Control.MAX_REPLY + 10)}
        fake = self.serve(lambda request: replies[request["cmd"]])
        control = cc.Control(fake.environ, timeout=0.3)
        for command, reason in (("refused", "failed"), ("garbage", "failed"), ("list", "failed"), ("closed", "failed"),
                                ("huge", "failed"), ("silent", "timeout")):
            with self.assertRaises(cc.ControlError, msg=command) as caught:
                control.request(command)
            self.assertEqual(caught.exception.reason, reason, command)
        with self.assertRaises(cc.ControlError) as caught:
            cc.Control(fake.environ, timeout=3).request("refused")       # (waits for the silent connection to end)
        self.assertIn("not available on this connection", str(caught.exception))

    def test_no_compositor(self):
        directory = tempfile.mkdtemp(prefix="bwT.", dir="/tmp")
        self.addCleanup(shutil.rmtree, directory, True)
        Path(directory, "boswas-file.sock").write_text("not a socket")
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(os.path.join(directory, "boswas-dead.sock"))
        dead.close()                                             # the file stays, nobody listens
        for environ in ({}, {"XDG_RUNTIME_DIR": directory}, {"XDG_RUNTIME_DIR": directory, "WAYLAND_DISPLAY": "none"},
                        {"XDG_RUNTIME_DIR": directory, "WAYLAND_DISPLAY": "file"},
                        {"XDG_RUNTIME_DIR": directory, "WAYLAND_DISPLAY": "dead"},
                        {"XDG_RUNTIME_DIR": directory, "WAYLAND_DISPLAY": "../x"},
                        {"XDG_RUNTIME_DIR": "relative", "WAYLAND_DISPLAY": "wayland-0"},
                        {"BOSWAS_COMPOSITOR_SOCKET": directory + "/missing.sock"}):
            with self.assertRaises(cc.ControlError, msg=environ) as caught:
                cc.Control(environ).reload_config()
            self.assertEqual(caught.exception.reason, "not-running", environ)
        self.assertIsNone(cc.control_socket({"XDG_RUNTIME_DIR": directory, "WAYLAND_DISPLAY": "a/b"}))
        self.assertEqual(cc.control_socket({"BOSWAS_COMPOSITOR_SOCKET": "/run/x.sock"}), "/run/x.sock")

    def test_outputs_are_validated(self):
        second = dict(HEADLESS[0], name="DP-10", x=1920, make="DEL", model="DELL U2720Q", scale=1.5,
                      rotation="90", width=1440, height=2560,
                      mode={"width": 3840, "height": 2160, "refresh": 59997},
                      modes=[{"width": 3840, "height": 2160, "refresh": 59997, "preferred": True},
                             {"width": 3840, "height": 2160, "refresh": 59997, "preferred": True},
                             {"width": 1920, "height": 1080, "refresh": 60000}, {"width": 0, "height": 5},
                             "junk", {"width": 1920, "height": 1080, "refresh": "fast"}])
        hostile = [None, 5, "x", {}, {"name": "bad name!"}, {"name": "A" * 65}, {"name": ["x"]},
                   {"name": "NOSIZE-1", "enabled": True}, {"name": "ODD-1", "width": "wide", "height": 5},
                   dict(HEADLESS[0], name="DP-2", scale="big", rotation="sideways", x="left", modes="none", mode=7,
                        make="A\x00B\x1b[31m" + "m" * 200, physical_width_mm=-5),
                   {"name": "eDP-1", "enabled": False, "width": 5},
                   dict(HEADLESS[0], name="DP-10", width=5)]              # a second entry of the same name
        outputs = cc.parse_outputs([second, *hostile, HEADLESS[0]])
        self.assertEqual([output.name for output in outputs], ["DP-2", "DP-10", "eDP-1", "HEADLESS-1"])
        odd, dell, internal, _ = outputs
        self.assertEqual((odd.scale, odd.rotation, odd.x, odd.modes, odd.mode, odd.physical_mm),
                         (1.0, "normal", 0, (), None, (0, 1080 * 0)))
        self.assertNotIn("\x00", odd.make)
        self.assertLessEqual(len(odd.make), 64)
        self.assertEqual((dell.label, dell.width, dell.height, dell.scale, dell.rotation),
                         ("DELL U2720Q", 1440, 2560, 1.5, "90"))
        self.assertEqual(dell.modes, (cc.Mode(3840, 2160, 59997, True), cc.Mode(1920, 1080, 60000)))
        self.assertEqual(dell.mode, cc.Mode(3840, 2160, 59997, True))
        self.assertEqual(dell.adaptive_sync, cc.AdaptiveSync(), "not reported: not offered")
        self.assertEqual((internal.enabled, internal.label, internal.modes), (False, "Built-in display", ()))
        for junk in (None, {}, "outputs", 7, [[[]]]):
            self.assertEqual(cc.parse_outputs(junk), [])
        self.assertEqual(len(cc.parse_outputs([dict(HEADLESS[0], name=f"DP-{n}") for n in range(40)])), 16)
        self.assertEqual(cc.parse_outputs(HEADLESS)[0].adaptive_sync, cc.AdaptiveSync(False, False))

    def test_variable_refresh_rate_is_read_as_the_compositor_reports_it(self):
        def sync(report):
            return cc.parse_outputs([dict(HEADLESS[0], adaptive_sync=report)])[0].adaptive_sync
        self.assertEqual(sync({"supported": True, "enabled": False}), cc.AdaptiveSync(True, False))
        self.assertEqual(sync({"supported": True, "enabled": True}), cc.AdaptiveSync(True, True))
        # On without being offered cannot be; anything but true is false; junk is "not offered".
        self.assertEqual(sync({"supported": False, "enabled": True}), cc.AdaptiveSync(False, False))
        for junk in ({"supported": "yes", "enabled": 1}, "on", None, [True], {}):
            self.assertEqual(sync(junk), cc.AdaptiveSync(), junk)

    def test_variable_refresh_rate_is_a_switch_of_each_monitor(self):
        self.assertEqual(cc.check_value("outputs.DP-1", "adaptive_sync", True), True)
        self.assertEqual(cc.check_value("outputs.DP-1", "adaptive_sync", False), False)
        for wrong in ("true", 1, None, "on"):
            with self.assertRaises(cc.InvalidValue, msg=repr(wrong)):
                cc.check_value("outputs.DP-1", "adaptive_sync", wrong)
        with self.assertRaises(cc.InvalidValue):
            cc.check_value("output", "adaptive_sync", True)            # per monitor only

    def test_apply_reports_whether_the_desktop_took_the_change(self):
        with tempfile.TemporaryDirectory() as home:
            environ = {"HOME": home}
            stand_in = StandIn()
            fake = self.serve(lambda request: {"ok": True, "data": {"warnings": []}})
            live = cc.Settings(environ, system_files=("/nonexistent/a", "/nonexistent/b"), write=stand_in.write,
                               remove=stand_in.remove, control=cc.Control(fake.environ))
            applied = live.apply({("keyboard", "numlock"): True})
            self.assertEqual((applied.live, applied.warnings, applied.problem, applied.message), (True, (), "", ""))
            self.assertEqual(fake.requests, [{"cmd": "reload-config"}])
            fake.answer = lambda request: {"ok": True, "data": {"warnings": ["outputs.X.mode is not WIDTHxHEIGHT"]}}
            self.assertIn("The desktop reported: outputs.X.mode", live.apply({("keyboard", "numlock"): False}).message)
            offline = cc.Settings(environ, system_files=("/nonexistent/a", "/nonexistent/b"), write=stand_in.write,
                                  remove=stand_in.remove, control=cc.Control({}))
            applied = offline.apply({("keyboard", "repeat_rate"): 20})
            self.assertEqual((applied.live, applied.problem), (False, cc.NOT_RUNNING))
            self.assertIn("next time you sign in", applied.message)
            self.assertEqual(tomllib.loads(Path(home, ".config/boswas/compositor.toml").read_text())["keyboard"],
                             {"numlock": False, "repeat_rate": 20})
            fake.answer = lambda request: {"ok": False, "error": "busy"}
            applied = live.apply({("keyboard", "repeat_rate"): 21})
            self.assertFalse(applied.live)
            self.assertIn("has not taken the change yet", applied.message)

    def test_the_functions_for_other_pages(self):
        with tempfile.TemporaryDirectory() as home:
            environ = {"HOME": home}
            self.assertEqual(cc.effective("power", "screen_off_seconds", environ=environ), 300)
            applied = cc.set_value("power", "screen_off_seconds", 900, environ=environ)       # the real writer
            self.assertFalse(applied.live)
            self.assertEqual(cc.effective("power", "screen_off_seconds", environ=environ), 900)
            cc.set_value("outputs.eDP-1", "scale", 1.5, environ=environ)
            self.assertEqual(cc.effective("outputs.eDP-1", "scale", environ=environ), 1.5)
            cc.set_value("power", "screen_off_seconds", None, environ=environ)
            self.assertEqual(cc.effective("power", "screen_off_seconds", environ=environ), 300)
            self.assertIsInstance(cc.effective("policy", "lock_idle_max_seconds", environ=environ), int)
            with self.assertRaises(cc.InvalidValue):
                cc.set_value("lock", "idle_seconds", 10 ** 9, environ=environ)
            self.assertIsInstance(cc.default(), cc.Settings)


# --- monitors -------------------------------------------------------------------------------------------------

class LayoutTest(unittest.TestCase):
    def test_modes(self):
        modes = [cc.Mode(3840, 2160, 60000, True), cc.Mode(3840, 2160, 29970), cc.Mode(1920, 1080, 60000),
                 cc.Mode(1920, 1080, 59940), cc.Mode(1920, 1080, 120000), cc.Mode(2560, 1440, 59951),
                 cc.Mode(1920, 1200, 60000)]
        self.assertEqual(cc.sizes(modes), [(3840, 2160), (2560, 1440), (1920, 1200), (1920, 1080)])
        self.assertEqual(cc.rates(modes, (1920, 1080)), [120000, 60000, 59940])
        self.assertEqual(cc.rates(modes, (640, 480)), [])
        self.assertEqual(cc.preferred_size(modes), (3840, 2160))
        self.assertIsNone(cc.preferred_size(modes[1:]))
        self.assertEqual([cc.rate_label(rate) for rate in (60000, 59940, 59951, 144000, 29970)],
                         ["60 Hz", "59.94 Hz", "59.95 Hz", "144 Hz", "29.97 Hz"])
        for width, height, refresh in ((2560, 1440, 59951), (1920, 1080, 60000), (1920, 1080, None)):
            text = cc.mode_text(width, height, refresh)
            self.assertEqual(cc.parse_mode(text), (width, height, refresh))
            self.assertEqual(cc.check_value("outputs.X", "mode", text), text)
        self.assertEqual(cc.mode_text(1920, 1080, 60000), "1920x1080@60")

    def test_logical_size_rounds_up_like_the_compositor(self):
        self.assertEqual(cc.logical_size(1920, 1080, 1.0), (1920, 1080))
        self.assertEqual(cc.logical_size(1920, 1080, 1.5), (1280, 720))          # what the real compositor reports
        self.assertEqual(cc.logical_size(1920, 1080, 1.75), (1098, 618))
        self.assertEqual(cc.logical_size(2560, 1440, 1.25), (2048, 1152))
        self.assertEqual(cc.logical_size(3840, 2160, 2.0, "90"), (1080, 1920))
        self.assertEqual(cc.logical_size(3840, 2160, 1.5, "270"), (1440, 2560))
        self.assertEqual(cc.logical_size(3840, 2160, 1.5, "180"), (2560, 1440))

    def test_rectangles(self):
        a = cc.Rect(0, 0, 100, 100)
        self.assertTrue(a.touches(cc.Rect(100, 50, 50, 50)) and a.touches(cc.Rect(-50, -20, 50, 50)))
        self.assertTrue(a.touches(cc.Rect(99, 100, 10, 10)) and a.touches(cc.Rect(0, -10, 1, 10)))
        self.assertFalse(a.touches(cc.Rect(100, 100, 50, 50)))                   # a corner is not an edge
        self.assertFalse(a.touches(cc.Rect(101, 0, 50, 50)) or a.touches(cc.Rect(50, 50, 10, 10)))
        self.assertTrue(a.overlaps(cc.Rect(99, 99, 10, 10)))
        self.assertFalse(a.overlaps(cc.Rect(100, 0, 10, 10)) or a.overlaps(cc.Rect(0, 100, 10, 10)))

    def test_a_drop_snaps_to_the_nearest_edge(self):
        big, small = cc.Rect(0, 0, 1920, 1080), cc.Rect(0, 0, 1280, 720)
        self.assertEqual(cc.snap(small, [big], 2100, 150), (1920, 150))           # right of it, the height kept
        self.assertEqual(cc.snap(small, [big], -1500, 900), (-1280, 900))         # left of it
        self.assertEqual(cc.snap(small, [big], -3000, 1200), (-1280, 1079))       # left, pulled up to keep an edge
        self.assertEqual(cc.snap(small, [big], 300, -900), (300, -720))           # above
        self.assertEqual(cc.snap(small, [big], 300, 1300), (300, 1080))           # below
        self.assertEqual(cc.snap(small, [big], 900, 400), (900, 1080))            # dropped on it: the nearest side
        self.assertEqual(cc.snap(small, [big], 1500, 300), (1920, 300))
        self.assertEqual(cc.snap(small, [big], 2000, 30, magnet=40), (1920, 0))   # tops in line
        self.assertEqual(cc.snap(small, [big], 2000, 390, magnet=40), (1920, 360))       # bottoms in line
        self.assertEqual(cc.snap(small, [big], 2000, 170, magnet=40), (1920, 180))       # middles in line
        self.assertEqual(cc.snap(small, [big], 2000, 100, magnet=40), (1920, 100))       # too far for the magnet
        self.assertEqual(cc.snap(small, [], 55, 66), (55, 66))

    def test_a_drop_never_lands_on_a_third_monitor(self):
        left, right = cc.Rect(0, 0, 1920, 1080), cc.Rect(1920, 0, 1920, 1080)
        moving = cc.Rect(0, 0, 1000, 600)
        for x, y in ((1900, 300), (1500, 200), (2000, -100), (960, 540), (3000, 900)):
            px, py = cc.snap(moving, [left, right], x, y)
            placed = moving.at(px, py)
            self.assertFalse(placed.overlaps(left) or placed.overlaps(right), (x, y, placed))
            self.assertTrue(placed.touches(left) or placed.touches(right), (x, y, placed))
        self.assertEqual(cc.snap(moving, [left, right], 1900, 300), (1900, 1080))        # below the seam
        self.assertEqual(cc.snap(moving, [left, right], 1900, 100), (1900, -600))        # above it

    def test_arrange_closes_gaps_and_resolves_overlaps(self):
        rects = {"A": cc.Rect(0, 0, 1920, 1080), "B": cc.Rect(1920, 0, 1920, 1080), "C": cc.Rect(3840, 0, 1280, 1024)}
        self.assertEqual(cc.arrange(rects, "A"), rects)                          # nothing to do
        self.assertTrue(cc.is_arranged(rects))
        smaller = dict(rects, A=cc.Rect(0, 0, 960, 540))                         # A was scaled to 200 %
        self.assertFalse(cc.is_arranged(smaller))
        fixed = cc.arrange(smaller, "A")
        self.assertEqual(fixed["A"], smaller["A"])
        self.assertTrue(cc.is_arranged(fixed))
        self.assertEqual((fixed["B"].x, fixed["C"].x), (960, 2880))
        larger = dict(rects, B=cc.Rect(1920, 0, 2560, 1440))                     # B got a bigger mode: it covers C
        fixed = cc.arrange(larger, "B")
        self.assertEqual((fixed["A"], fixed["B"]), (larger["A"], larger["B"]))
        self.assertTrue(cc.is_arranged(fixed))
        self.assertEqual(cc.arrange({"A": rects["A"]}, "A"), {"A": rects["A"]})
        self.assertEqual(list(cc.arrange(rects, "C")), ["A", "B", "C"])

    def test_dropping_the_middle_monitor_elsewhere_closes_the_row(self):
        rects = {"A": cc.Rect(0, 0, 1920, 1080), "B": cc.Rect(1920, 0, 1920, 1080), "C": cc.Rect(3840, 0, 1920, 1080)}
        result = cc.drop(rects, "B", 6000, 0)
        self.assertTrue(cc.is_arranged(result))
        self.assertEqual(result["B"], cc.Rect(5760, 0, 1920, 1080))
        self.assertEqual(result["C"], rects["C"])                                # only A had to move
        self.assertEqual(result["A"].right, result["C"].x)
        swapped = cc.drop({"A": rects["A"], "B": rects["B"]}, "B", -2500, 40, magnet=60)
        self.assertEqual(swapped, {"A": rects["A"], "B": cc.Rect(-1920, 0, 1920, 1080)})

    def test_positions_stay_in_the_compositors_range(self):
        rects = {"A": cc.Rect(-32000, 0, 3840, 2160), "B": cc.Rect(-28160, 0, 3840, 2160)}
        result = cc.drop(rects, "B", -40000, 0)
        self.assertTrue(cc.is_arranged(result))
        for rect in result.values():
            self.assertEqual(cc.check_value("outputs.X", "position", [rect.x, rect.y]), [rect.x, rect.y])
        self.assertEqual(min(rect.x for rect in result.values()), 0)

    def test_random_drops_always_give_a_legal_arrangement(self):
        rng = random.Random(20261115)
        sizes = [(1920, 1080), (2560, 1440), (1280, 720), (1080, 1920), (1098, 618), (3840, 2160), (1366, 768)]
        for _ in range(300):
            count = rng.randint(2, 5)
            rects, x = {}, 0
            for index in range(count):
                width, height = rng.choice(sizes)
                rects[f"M{index}"] = cc.Rect(x, rng.randint(-200, 200), width, height)
                x += width
            self.assertTrue(cc.is_arranged(rects))
            for _ in range(4):
                name = rng.choice(list(rects))
                rects = cc.drop(rects, name, rng.randint(-6000, 9000), rng.randint(-4000, 5000), rng.choice((0, 40)))
                self.assertTrue(cc.is_arranged(rects), rects)
                changed = rng.choice(list(rects))                               # then one changes its size
                rects[changed] = cc.Rect(rects[changed].x, rects[changed].y, *rng.choice(sizes))
                rects = cc.arrange(rects, changed)
                self.assertTrue(cc.is_arranged(rects), rects)


# --- the real compositor (opt-in) -----------------------------------------------------------------------------

@unittest.skipUnless(os.environ.get("BOSWAS_TEST_COMPOSITOR"), "set BOSWAS_TEST_COMPOSITOR=1 to start the compositor")
@unittest.skipUnless(COMPOSITOR.exists(), f"{COMPOSITOR} is not built")
class RealCompositorTest(unittest.TestCase):
    """Settings written by this module, read by the real compositor (headless, private directories)."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="cc-real-")
        self.runtime = tempfile.mkdtemp(prefix="bwT.", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.addCleanup(shutil.rmtree, self.runtime, True)
        self.environ = {"HOME": self.home, "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
                        "XDG_RUNTIME_DIR": self.runtime}
        self.settings = cc.Settings(self.environ)                 # the real files, the real writer
        self.process = None

    def start(self) -> None:
        environ = {key: value for key, value in os.environ.items()
                   if key not in ("WAYLAND_DISPLAY", "DISPLAY", "BOSWAS_COMPOSITOR_SOCKET")}
        environ.update(self.environ)
        self.process = subprocess.Popen([str(COMPOSITOR), "--backend", "headless", "--no-shell", "--print-env"],
                                        env=environ, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.stop)
        for _ in range(2):
            name, _, value = self.process.stdout.readline().strip().partition("=")
            self.environ[name] = value
        self.assertIn("WAYLAND_DISPLAY", self.environ)
        self.settings = cc.Settings(self.environ)

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process.stdout.close()

    def test_a_scale_written_here_is_what_the_compositor_uses(self):
        self.settings.update({("outputs.HEADLESS-1", "scale"): 1.5})
        self.start()
        self.assertEqual(self.settings.control.socket_path(), self.environ["BOSWAS_COMPOSITOR_SOCKET"])
        output = self.settings.outputs()[0]
        self.assertEqual((output.name, output.scale), ("HEADLESS-1", 1.5))
        self.assertEqual((output.width, output.height), cc.logical_size(1920, 1080, 1.5))
        self.assertEqual(output.mode, cc.Mode(1920, 1080, 60000, True))
        self.assertEqual(self.settings.reload(), [])

    def test_every_key_this_module_writes_is_accepted(self):
        self.start()
        self.assertEqual(self.settings.reload(), [])
        changes = {("keyboard", "layout"): "us,de,fr,in", ("keyboard", "variant"): ",neo,,",
                   ("keyboard", "model"): "pc105", ("keyboard", "options"): "grp:alt_shift_toggle,caps:escape",
                   ("keyboard", "repeat_delay"): 250, ("keyboard", "repeat_rate"): 40, ("keyboard", "numlock"): True,
                   ("pointer", "natural_scroll"): True, ("pointer", "accel_speed"): -0.35,
                   ("pointer", "accel_profile"): "flat", ("pointer", "left_handed"): True,
                   ("pointer", "cursor_theme"): "Breeze_Light", ("pointer", "cursor_size"): 48,
                   ("touchpad", "tap_to_click"): False, ("touchpad", "natural_scroll"): False,
                   ("touchpad", "accel_speed"): 1, ("touchpad", "disable_while_typing"): False,
                   ("touchpad", "gestures"): False, ("output", "scale"): 1.25,
                   ("outputs.HEADLESS-1", "enabled"): True, ("outputs.HEADLESS-1", "mode"): "1920x1080@60",
                   ("outputs.HEADLESS-1", "scale"): 1.75, ("outputs.HEADLESS-1", "position"): [0, 0],
                   ("outputs.HEADLESS-1", "rotation"): "normal", ("outputs.HEADLESS-1", "adaptive_sync"): True,
                   ("outputs.DP-7", "enabled"): False,
                   ("outputs.DP-7", "position"): [-1920, 120], ("outputs.DP-7", "rotation"): "270",
                   ("night_light", "enabled"): True, ("night_light", "temperature"): 3400,
                   ("night_light", "scheduled"): True, ("night_light", "from"): "21:30", ("night_light", "to"): "07:15",
                   ("power", "screen_off_seconds"): 900, ("lock", "idle_seconds"): 300,
                   ("xwayland", "enabled"): True, ("apps", "terminal"): ["foot", "--title", "a \"b\" \\ c"],
                   ("apps", "files"): ["boswas-files"], ("apps", "settings"): ["boswas-control-center"],
                   ("apps", "task_manager"): ["boswas-task-manager"],
                   ("appearance", "scheme"): "light", ("appearance", "reduced_motion"): True,
                   ("appearance", "wallpaper"): "/usr/share/wallpapers/Sea ä \"x\".png"}
        applied = self.settings.apply(changes)
        self.assertTrue(applied.live)
        self.assertEqual(applied.warnings, ())
        snapshot = self.settings.snapshot()
        self.assertEqual(snapshot.problems(), [])
        for (table, key), value in changes.items():
            self.assertEqual(snapshot.get(table, key), value, (table, key))
        # What the compositor refuses, this module refuses to write; what it would warn about, too.
        for seconds in (0, 3600):
            with self.assertRaises(cc.InvalidValue):
                self.settings.apply({("lock", "idle_seconds"): seconds})
        self.assertEqual(self.settings.reload(), [])
        # Back to no user files at all.
        self.settings.restore(None)
        self.settings.update({("appearance", "scheme"): None, ("appearance", "wallpaper"): None})
        self.assertEqual(self.settings.reload(), [])
        self.assertEqual(self.settings.snapshot().values, cc.defaults())

    def test_the_compositor_discards_a_configuration_with_an_unknown_key(self):
        self.start()
        directory = Path(self.settings.directory())
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "compositor.toml").write_text("[keyboard]\nlayuot = \"us\"\n")
        warnings = self.settings.reload()
        self.assertEqual(len(warnings), 1)
        self.assertIn("ignored", warnings[0])
        self.assertIn("compositor.toml", warnings[0])
        self.assertEqual(self.settings.snapshot().layer("user").state, "rejected")
        applied = self.settings.apply({("keyboard", "numlock"): True})          # Settings repairs the file
        self.assertEqual(applied.warnings, ())
        self.assertTrue((directory / "compositor.toml.bak").exists())


if __name__ == "__main__":
    unittest.main()
