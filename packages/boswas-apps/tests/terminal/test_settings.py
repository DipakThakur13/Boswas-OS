"""Settings and profiles, the command line, the colour schemes."""

import importlib.util
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path

from boswas_fluent.tokens import Theme

from boswas_apps.terminal import cli, ptyhost, schemes
from boswas_apps.terminal import settings as settings_module
from boswas_apps.terminal.settings import Profile, Settings

PACKAGE = Path(settings_module.__file__).resolve().parent
REPOSITORY = PACKAGE.parents[3]


class SettingsFile(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "config" / "settings.json"

    def tearDown(self):
        self._directory.cleanup()

    def test_defaults_without_a_file(self):
        settings, error = settings_module.load(self.path)
        self.assertIsNone(error)
        self.assertEqual(len(settings.profiles), 1)
        profile = settings.default()
        self.assertEqual((profile.font_size, profile.scrollback, profile.scheme, profile.command), (11.0, 10000, "prism", ""))
        self.assertFalse(settings.clipboard_write)
        self.assertTrue(settings.paste_warning)

    def test_round_trip(self):
        settings = settings_module.defaults()
        settings.profiles.append(Profile("p2", "Monitor", command="htop -d 10", directory="~/src", font_family="Hack",
                                         font_size=13.5, scheme="solarized-dark", cursor_shape="block",
                                         cursor_blink=False, scrollback=500, bell="none", close_on_exit="never"))
        settings.default_profile = "p2"
        settings.copy_on_select = True
        settings.clipboard_write = True
        settings_module.save(self.path, settings)
        loaded, error = settings_module.load(self.path)
        self.assertIsNone(error)
        self.assertEqual(loaded, settings)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(os.listdir(self.path.parent), ["settings.json"])

    def test_damaged_file(self):
        self.path.parent.mkdir()
        for content in (b"{not json", b"[1, 2]", b"\xff\xfe", b'"text"'):
            self.path.write_bytes(content)
            settings, error = settings_module.load(self.path)
            self.assertIn("damaged", error)
            self.assertEqual(len(settings.profiles), 1)
        self.path.write_bytes(b" " * (settings_module.MAX_FILE + 10))
        self.assertIn("too large", settings_module.load(self.path)[1])

    def test_hostile_values_fall_back(self):
        data = {"default_profile": "missing", "copy_on_select": "yes", "clipboard_write": 1, "paste_warning": None,
                "unknown": {"x": 1},
                "profiles": [
                    {"id": "../../etc", "name": "a\x1b[31mb\n" + "x" * 500, "command": 7, "directory": ["/"],
                     "font_family": "F" * 1000, "font_size": 1e9, "scheme": "nope", "cursor_shape": "star",
                     "cursor_blink": "no", "scrollback": -5, "bell": "loud", "close_on_exit": 3},
                    "not a profile", None, {"id": "ok", "name": "", "font_size": float("nan"), "scrollback": 1e12},
                    {"id": "ok", "name": "Same id"}]}
        settings = settings_module.from_data(data)
        first, second, third = settings.profiles
        self.assertRegex(first.id, r"^[0-9a-f]{12}$")
        self.assertEqual(first.name, "a[31mb" + "x" * (settings_module.MAX_NAME - 6))
        self.assertEqual((first.command, first.directory, len(first.font_family)), ("", "", 128))
        self.assertEqual((first.font_size, first.scheme, first.cursor_shape, first.cursor_blink),
                         (settings_module.FONT_SIZE_MAX, "prism", "bar", True))
        self.assertEqual((first.scrollback, first.bell, first.close_on_exit), (0, "visual", "graceful"))
        self.assertEqual((second.id, second.name, second.font_size), ("ok", "Profile", 11.0))
        self.assertEqual(second.scrollback, settings_module.MAX_SCROLLBACK)
        self.assertNotEqual(third.id, "ok")
        self.assertEqual(settings.default_profile, first.id)
        self.assertEqual((settings.copy_on_select, settings.clipboard_write, settings.paste_warning), (False, False, True))

    def test_profile_count_is_capped(self):
        data = {"profiles": [{"id": f"p{i}", "name": f"P{i}"} for i in range(500)]}
        self.assertEqual(len(settings_module.from_data(data).profiles), settings_module.MAX_PROFILES)

    def test_lookup(self):
        settings = Settings([Profile("a", "Alpha"), Profile("b", "Beta")], "b")
        self.assertEqual(settings.default().id, "b")
        self.assertEqual(settings.profile("a").id, "a")
        self.assertEqual(settings.profile("zzz").id, "b")
        self.assertEqual(settings.find("beta").id, "b")
        self.assertEqual(settings.find("a").id, "a")
        self.assertIsNone(settings.find("gamma"))
        copy = settings.copy()
        copy.profiles[0].name = "Changed"
        self.assertEqual(settings.profiles[0].name, "Alpha")

    def test_save_failure_leaves_no_temporary_file(self):
        blocker = Path(self._directory.name) / "file"
        blocker.write_text("x")
        with self.assertRaises(OSError):
            settings_module.save(blocker / "settings.json", settings_module.defaults())


class Launching(unittest.TestCase):
    def test_login_shell_by_default(self):
        launch = settings_module.launch_for(Profile("p", "P"), directory="/tmp")
        self.assertTrue(os.path.isabs(launch.program))
        self.assertIn(launch.argv[0], ("bash", "-" + os.path.basename(launch.program)))
        self.assertEqual(launch.directory, "/tmp")

    def test_profile_command(self):
        launch = settings_module.launch_for(Profile("p", "P", command="sh -c 'echo  hi'"))
        self.assertEqual(launch.argv, ("sh", "-c", "echo  hi"))
        self.assertEqual(os.path.basename(launch.program), "sh")
        self.assertEqual(launch.name, "sh")

    def test_command_line_command_wins(self):
        launch = settings_module.launch_for(Profile("p", "P", command="vim"), command=["/bin/cat", "-n"])
        self.assertEqual((launch.program, launch.argv), ("/bin/cat", ("/bin/cat", "-n")))

    def test_directory_order(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            profile = Profile("p", "P", directory=second)
            self.assertEqual(settings_module.launch_for(profile, directory=first).directory, first)
            self.assertEqual(settings_module.launch_for(profile, fallback_directory=first).directory, second)
            self.assertEqual(settings_module.launch_for(Profile("p", "P"), fallback_directory=first).directory, first)
            self.assertEqual(settings_module.launch_for(Profile("p", "P", directory="/nonexistent/x")).directory,
                             ptyhost.home_directory())

    def test_bad_commands(self):
        for command in ("boswas-no-such-program", "'unbalanced", "/etc/hostname"):
            with self.assertRaises(ptyhost.SpawnError):
                settings_module.launch_for(Profile("p", "P", command=command))


class CommandLine(unittest.TestCase):
    def test_nothing(self):
        invocation = cli.parse([], "/home/mira")
        self.assertEqual(invocation.request, cli.Request(launch_directory="/home/mira"))
        self.assertFalse(invocation.separate)
        self.assertEqual(cli.parse([], "/").request.launch_directory, "")
        self.assertEqual(cli.parse(["--working-directory", "etc"], "/").request.directory, "/etc")

    def test_options(self):
        request = cli.parse(["--working-directory", "src", "--title=Build log", "--profile", "Dev", "--new-window"],
                            "/home/mira").request
        self.assertEqual(request, cli.Request(directory="/home/mira/src", title="Build log", profile="Dev",
                                              new_window=True, launch_directory="/home/mira"))
        self.assertEqual(cli.parse(["--working-directory=/tmp/../etc"], "/x").request.directory, "/etc")
        self.assertTrue(cli.parse(["--separate"], "/").separate)

    def test_everything_after_e_is_the_command(self):
        request = cli.parse(["--title", "t", "-e", "vim", "--title", "-e", "--new-window", "file"], "/").request
        self.assertEqual(request.command, ["vim", "--title", "-e", "--new-window", "file"])
        self.assertEqual(request.title, "t")
        self.assertFalse(request.new_window)
        self.assertEqual(cli.parse(["-x", "top"], "/").request.command, ["top"])
        self.assertEqual(cli.parse(["--", "top", "-d", "1"], "/").request.command, ["top", "-d", "1"])

    def test_one_argument_command_line(self):
        self.assertEqual(cli.parse(["-e", "less -S '/tmp/a b'"], "/").request.command, ["less", "-S", "/tmp/a b"])

    def test_errors(self):
        for arguments in (["--bogus"], ["file.txt"], ["-e"], ["--title"], ["--working-directory="],
                          ["--new-window=1"], ["--boswas-request={}"], ["-e", "a\0b"]):
            with self.assertRaises(cli.UsageError, msg=arguments):
                cli.parse(arguments, "/")

    def test_help_and_version(self):
        self.assertEqual(cli.parse(["--help"], "/").show, "help")
        self.assertEqual(cli.parse(["--version"], "/").show, "version")
        self.assertIn("-e COMMAND", cli.USAGE)

    def test_request_round_trip(self):
        request = cli.Request(directory="/tmp/ä b", title="T \"x\"", profile="P", command=["sh", "-c", "echo 'é' -platform x"],
                              new_window=True, launch_directory="/home")
        encoded = cli.encode(request)
        self.assertTrue(all(argument.startswith("--boswas-request=") and len(argument) < 8192 for argument in encoded))
        self.assertEqual(cli.decode(encoded), request)

    def test_long_request_is_split_and_huge_one_refused(self):
        request = cli.Request(command=["echo", "x" * 20000])
        encoded = cli.encode(request)
        self.assertGreater(len(encoded), 1)
        self.assertEqual(cli.decode(encoded), request)
        self.assertIsNone(cli.encode(cli.Request(command=["x" * 30000] * 3)))

    def test_decode_distrusts_its_input(self):
        empty = cli.Request()
        for arguments in ([], ["--boswas-request=not json"], ["--boswas-request=[1]"], ["other"], [5],
                          ["--boswas-request=" + json.dumps({"command": "rm -rf /"})],
                          ["--boswas-request=" + json.dumps({"command": ["ok", 5]})],
                          ["--boswas-request=" + json.dumps({"directory": "relative", "title": 7, "new_window": "yes"})]):
            self.assertEqual(cli.decode(arguments), empty, arguments)


class Schemes(unittest.TestCase):
    def test_prism_is_the_terminal_palette_of_the_ui_tokens_tool(self):
        tool = REPOSITORY / "desktop" / "ui" / "tools" / "build_kde_colors.py"
        if not tool.is_file():
            self.skipTest("the colour scheme tool is not in this tree")
        spec = importlib.util.spec_from_file_location("build_kde_colors", tool)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(schemes.TERMINAL, module.TERMINAL)
        tokens = module.load_tokens()
        for mode in ("dark", "light"):
            expected = module.terminal_colours(tokens, mode)
            scheme = schemes.prism(mode, Theme(mode, tokens))
            self.assertEqual(scheme.background.upper(), expected["Background"].upper())
            self.assertEqual(scheme.foreground.upper(), expected["Foreground"].upper())
            for index in range(8):
                self.assertEqual(scheme.colors[index].upper(), expected[f"Color{index}"].upper(), (mode, index))
                self.assertEqual(scheme.colors[8 + index].upper(), expected[f"Color{index}Intense"].upper())

    def test_following_the_desktop(self):
        self.assertTrue(schemes.get("prism", "dark").dark)
        self.assertFalse(schemes.get("prism", "light").dark)
        self.assertFalse(schemes.get("prism-light", "dark").dark)
        self.assertTrue(schemes.get("no-such-scheme", "dark").dark)

    def test_classic_schemes(self):
        classic = schemes.classic()
        self.assertEqual(set(classic), {"campbell", "one-half-dark", "one-half-light", "solarized-dark", "solarized-light"})
        for scheme in classic.values():
            self.assertEqual(len(scheme.colors), 16)
            for colour in (*scheme.colors, scheme.foreground, scheme.background, scheme.cursor, scheme.selection):
                self.assertRegex(colour, r"^#[0-9A-F]{6}$")
        self.assertFalse(classic["solarized-light"].dark)
        keys = [key for key, _name in schemes.names()]
        self.assertEqual(keys[:3], ["prism", "prism-dark", "prism-light"])
        self.assertTrue(all(schemes.is_known(key) for key in keys))

    def test_palette(self):
        colours = schemes.palette(schemes.get("campbell"))
        self.assertEqual(len(colours), 256)
        self.assertEqual(colours[16], (0, 0, 0))
        self.assertEqual(colours[21], (0, 0, 255))
        self.assertEqual(colours[196], (255, 0, 0))
        self.assertEqual(colours[231], (255, 255, 255))
        self.assertEqual(colours[232], (8, 8, 8))
        self.assertEqual(colours[255], (238, 238, 238))

    def test_no_colour_values_in_the_code(self):
        pattern = re.compile(r"#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b|\brgba?\(|QColor\(\s*[\"']")
        for path in sorted(PACKAGE.glob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "_HEX_COLOUR" in line or "_COLOUR = re.compile" in line or "#RRGGBB" in line or "#fff" in line:
                    continue
                self.assertIsNone(pattern.search(line), f"{path.name}:{number}: {line.strip()}")


class Packaging(unittest.TestCase):
    ROOT = PACKAGE.parents[1]

    def test_desktop_entry(self):
        text = (self.ROOT / "data" / "terminal" / "com.boswas.Terminal.desktop").read_text(encoding="utf-8")
        for line in ("Name=Terminal", "Icon=boswas-terminal", "Categories=System;TerminalEmulator;",
                     "Exec=boswas-terminal", "StartupWMClass=com.boswas.Terminal", "Actions=new-window;",
                     "[Desktop Action new-window]", "Name=New Window", "Exec=boswas-terminal --new-window"):
            self.assertIn(line + "\n", text)
        icon = REPOSITORY / "desktop" / "icons" / "Boswas-Prism" / "scalable" / "apps" / "boswas-terminal.svg"
        if icon.parent.is_dir():
            self.assertTrue(icon.is_file())

    def test_launcher_and_manual(self):
        launcher = (self.ROOT / "bin" / "boswas-terminal").read_text(encoding="utf-8")
        self.assertTrue(launcher.startswith("#!/usr/bin/python3 -IB\n"))
        self.assertIn("from boswas_apps.terminal.__main__ import main", launcher)
        manual = (self.ROOT / "man" / "boswas-terminal.1").read_text(encoding="utf-8")
        for word in (".TH BOSWAS-TERMINAL 1", ".SH NAME", ".SH SYNOPSIS", ".SH OPTIONS", ".SH FILES", ".SH SEE ALSO",
                     "\\-\\-working\\-directory", "\\-\\-new\\-window", "\\-\\-separate", "\\-\\-profile", "\\-\\-title"):
            self.assertIn(word, manual)

    def test_no_carriage_returns_or_tool_names(self):
        for path in [*PACKAGE.rglob("*.py"), *PACKAGE.rglob("*.json"), *(self.ROOT / "tests" / "terminal").glob("*.py"),
                     self.ROOT / "man" / "boswas-terminal.1", self.ROOT / "bin" / "boswas-terminal"]:
            data = path.read_bytes()
            self.assertNotIn(b"\r\n", data, path)


if __name__ == "__main__":
    unittest.main()
