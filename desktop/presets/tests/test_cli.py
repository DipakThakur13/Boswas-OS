"""boswas-preset: list, current, apply and reset, in a temporary home."""

import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import support

sys.path.insert(0, str(support.PRESETS_DIR))
from boswas_preset import cli  # noqa: E402

PEARL_SCHEME = ("[ColorEffects:Disabled]\nColor=1,2,3\n\n[Colors:Window]\nBackgroundNormal=250,250,250\n\n"
                "[General]\nName=Boswas Pearl\n\n[WM]\nactiveBackground=1,1,1\n")
PRISM_SCHEME = ("[Colors:Window]\nBackgroundNormal=18,16,28\n\n[General]\nName=Boswas Prism\n\n"
                "[WM]\nactiveBackground=26,23,40\n")
OLD_KDEGLOBALS = ("[General]\nColorScheme=Old\nfont=Inter,10\n\n[Colors:Window]\nBackgroundNormal=0,0,0\n\n"
                  "[Icons]\nTheme=Boswas-Prism\n")


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.config = self.home / "config"
        self.config.mkdir()
        self.env = {
            "BOSWAS_PRESETS_FILE": str(support.PRESETS_FILE),
            "BOSWAS_COLOR_SCHEMES": str(support.PRESETS_DIR / "color-schemes"),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv, euid=1000, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), env=env or self.env, geteuid=lambda: euid, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def schemes(self, **files) -> dict:
        """An environment whose colour schemes are the given files (name=text)."""
        directory = self.home / "schemes"
        directory.mkdir(exist_ok=True)
        for name, text in files.items():
            (directory / f"{name}.colors").write_text(text, encoding="utf-8")
        return dict(self.env, BOSWAS_COLOR_SCHEMES=str(directory))

    def tree(self) -> dict:
        """Everything below the config directory: {relative path: bytes, link target or None for a directory}."""
        found = {}
        for path in sorted(self.config.rglob("*")):
            name = path.relative_to(self.config).as_posix()
            if path.is_symlink():
                found[name] = "-> " + os.readlink(path)
            elif path.is_dir():
                found[name] = None
            else:
                found[name] = path.read_bytes()
        return found


class ListTest(CliTestCase):
    def test_json_list(self):
        code, out, err = self.run_cli("--json", "list")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(set(data), {"presets", "current"})
        self.assertIsNone(data["current"])
        self.assertEqual([p["id"] for p in data["presets"]], support.PRESET_IDS)
        for item, preset in zip(data["presets"], support.presets()):
            self.assertEqual(set(item), {"id", "name", "description", "variant", "accent", "preview"})
            self.assertEqual(item["name"], preset["name"])
            self.assertEqual(item["description"], preset["description"])
            self.assertEqual(item["variant"], preset["variant"])
            self.assertEqual(item["accent"], preset["palette"]["accent"])
            self.assertRegex(item["accent"], support.HEX)
            self.assertEqual(item["preview"], f"/usr/share/boswas/presets/previews/{preset['id']}.png")
        self.assertEqual(self.tree(), {}, "listing writes nothing")

    def test_json_flag_after_the_command(self):
        code, out, _ = self.run_cli("list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["presets"]), 10)

    def test_text_list_marks_the_current_preset(self):
        (self.config / "boswasrc").write_text("[Preset]\nCurrent=ember\n", encoding="utf-8")
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 10)
        self.assertTrue(lines[7].startswith("* ember"))

    def test_unreadable_presets_file(self):
        env = dict(self.env, BOSWAS_PRESETS_FILE=str(self.home / "missing.json"))
        code, out, err = self.run_cli("--json", "list", env=env)
        self.assertEqual(code, 1)
        self.assertIn("missing.json", err)
        self.assertEqual(json.loads(out)["code"], 1)

    def test_presets_file_is_validated(self):
        """Ids and labels become parts of file names; nothing odd gets that far."""
        good = support.presets()[0]
        for change in ({"id": "../x"}, {"id": "Ocean"}, {"id": "a\nb"}, {"id": 7}, {"label": "../../etc"},
                       {"label": "Two Words"}, {"label": ""}, {"variant": "sepia"}, {"name": "a\nb"},
                       {"description": None}, {"palette": {}}, {"palette": {"accent": "red"}}, {"palette": "x"}):
            path = self.home / "presets.json"
            path.write_text(json.dumps({"presets": [dict(good, **change)]}), encoding="utf-8")
            with self.subTest(change=change):
                code, _out, err = self.run_cli("list", env=dict(self.env, BOSWAS_PRESETS_FILE=str(path)))
                self.assertEqual(code, 1)
                self.assertIn("cannot read the presets file", err)
        for document in ({"presets": [good, good]}, {"presets": {"a": good}}, {"presets": ["horizon"]}, [], {}):
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.subTest(document=str(document)[:40]):
                self.assertEqual(self.run_cli("list", env=dict(self.env, BOSWAS_PRESETS_FILE=str(path)))[0], 1)


class CurrentTest(CliTestCase):
    def test_nothing_recorded(self):
        code, out, _ = self.run_cli("current")
        self.assertEqual((code, out), (0, ""))
        code, out, _ = self.run_cli("--json", "current")
        self.assertEqual(json.loads(out), {"current": None})

    def test_recorded_by_an_earlier_version(self):
        """Earlier versions wrote the file with KDE's configuration tool: the same format."""
        (self.config / "boswasrc").write_text("# comment\n[Other]\nCurrent=x\n\n[Preset]\nCurrent=nebula\n",
                                              encoding="utf-8")
        code, out, _ = self.run_cli("current")
        self.assertEqual((code, out), (0, "nebula\n"))
        code, out, _ = self.run_cli("current", "--json")
        self.assertEqual(json.loads(out), {"current": "nebula"})
        (self.config / "boswasrc").write_text("[Preset]\nCurrent[$i]=slate\n", encoding="utf-8")
        self.assertEqual(self.run_cli("current")[1], "slate\n")

    def test_unknown_recorded_id_is_ignored(self):
        (self.config / "boswasrc").write_text("[Preset]\nCurrent=bogus\n", encoding="utf-8")
        code, out, _ = self.run_cli("--json", "current")
        self.assertEqual(json.loads(out), {"current": None})

    def test_a_global_theme_setting_is_not_a_preset(self):
        """Only the recorded choice counts: there are no Global Themes to be active."""
        system = self.home / "xdg"
        system.mkdir()
        for directory in (system, self.config):
            (directory / "kdeglobals").write_text("[KDE]\nLookAndFeelPackage=com.boswas.horizon\n", encoding="utf-8")
        _, out, _ = self.run_cli("--json", "current", env=dict(self.env, XDG_CONFIG_DIRS=str(system)))
        self.assertEqual(json.loads(out), {"current": None})

    def test_default_config_home(self):
        (self.home / ".config").mkdir()
        (self.home / ".config" / "boswasrc").write_text("[Preset]\nCurrent=slate\n", encoding="utf-8")
        for value in (None, "", "relative/config"):        # XDG: a path that is not absolute is ignored
            env = {k: v for k, v in self.env.items() if k != "XDG_CONFIG_HOME"}
            if value is not None:
                env["XDG_CONFIG_HOME"] = value
            with self.subTest(XDG_CONFIG_HOME=value):
                self.assertEqual(self.run_cli("current", env=env)[1], "slate\n")


class ApplyTest(CliTestCase):
    def test_apply_writes_the_three_files(self):
        for preset in support.presets():
            with self.subTest(preset=preset["id"]):
                code, out, err = self.run_cli("apply", preset["id"])
                self.assertEqual((code, err), (0, ""))
                self.assertIn(preset["name"], out)
                appearance = tomllib.loads((self.config / "boswas" / "appearance.toml").read_text(encoding="utf-8"))
                images = "images_dark" if preset["variant"] == "dark" else "images"
                self.assertEqual(appearance, {"appearance": {
                    "scheme": preset["variant"],
                    "wallpaper": f"/usr/share/wallpapers/Boswas-{preset['label']}/contents/{images}/3840x2160.png"}})
                merged = support.parse_ini(self.config / "kdeglobals")
                scheme = support.parse_ini(support.PRESETS_DIR / "color-schemes" / f"Boswas{preset['label']}.colors")
                self.assertEqual(merged["General"], {"ColorScheme": f"Boswas{preset['label']}"})
                for group in scheme:
                    if group.startswith("Colors:") or group == "WM":
                        self.assertEqual(merged[group], scheme[group], group)
                self.assertEqual((self.config / "boswasrc").read_text(encoding="utf-8"),
                                 f"[Preset]\nCurrent={preset['id']}\n")
                self.assertEqual(self.run_cli("current")[1], preset["id"] + "\n")
        self.assertEqual(sorted(self.tree()), ["boswas", "boswas/appearance.toml", "boswasrc", "kdeglobals"],
                         "nothing else is written, no temporary file is left behind")
        for name in ("boswas/appearance.toml", "boswasrc", "kdeglobals"):
            self.assertEqual(stat.S_IMODE((self.config / name).stat().st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE((self.config / "boswas").stat().st_mode), 0o700)

    def test_json_and_the_desktop_name_do_not_matter(self):
        for desktop in ("Boswas", "KDE", "", None):
            env = dict(self.env)
            if desktop is not None:
                env["XDG_CURRENT_DESKTOP"] = desktop
            with self.subTest(desktop=desktop):
                code, out, err = self.run_cli("--json", "apply", "horizon", env=env)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(json.loads(out), {"applied": "horizon"})
                self.assertIn('scheme = "dark"', (self.config / "boswas" / "appearance.toml").read_text())

    def test_the_panel_layout_option_is_gone(self):
        code, out, err = self.run_cli("--json", "apply", "classic", "--layout")
        self.assertEqual(code, 3)
        self.assertIn("usage: boswas-preset [--json] {list,current,apply ID,reset}", err)
        self.assertEqual(json.loads(out)["code"], 3)
        self.assertEqual(self.tree(), {})

    def test_colours_are_merged_into_the_users_kdeglobals(self):
        env = self.schemes(BoswasPearl=PEARL_SCHEME)
        (self.config / "kdeglobals").write_text(OLD_KDEGLOBALS, encoding="utf-8")
        code, out, err = self.run_cli("--json", "apply", "pearl", env=env)
        self.assertEqual((code, err), (0, ""))
        appearance = (self.config / "boswas" / "appearance.toml").read_text(encoding="utf-8")
        self.assertIn('scheme = "light"', appearance)
        self.assertIn('wallpaper = "/usr/share/wallpapers/Boswas-Pearl/contents/images/3840x2160.png"', appearance)
        self.assertEqual(set(tomllib.loads(appearance)), {"appearance"})
        merged = support.parse_ini(self.config / "kdeglobals")
        self.assertEqual(merged["General"]["ColorScheme"], "BoswasPearl")
        self.assertEqual(merged["General"]["font"], "Inter,10", "other settings are kept")
        self.assertEqual(merged["Colors:Window"]["BackgroundNormal"], "250,250,250")
        self.assertEqual(merged["WM"]["activeBackground"], "1,1,1")
        self.assertEqual(merged["Icons"]["Theme"], "Boswas-Prism")
        self.assertNotIn("ColorEffects:Disabled", merged, "only colour groups are copied")

    def test_bytes_that_are_not_utf8_survive_the_merge(self):
        env = self.schemes(BoswasPearl=PEARL_SCHEME)
        (self.config / "kdeglobals").write_bytes(b"[Odd]\nname=caf\xe9\nline=a\xe2\x80\xa8b\n\n[General]\nfont=Inter,10\n")
        (self.config / "boswasrc").write_bytes(b"[Odd]\nname=caf\xe9\n")
        self.assertEqual(self.run_cli("apply", "pearl", env=env)[0], 0)
        kdeglobals = (self.config / "kdeglobals").read_bytes()
        self.assertIn(b"[Odd]\nname=caf\xe9\nline=a\xe2\x80\xa8b\n", kdeglobals)
        self.assertIn(b"[General]\nColorScheme=BoswasPearl\nfont=Inter,10\n", kdeglobals)
        self.assertEqual((self.config / "boswasrc").read_bytes(), b"[Odd]\nname=caf\xe9\n\n[Preset]\nCurrent=pearl\n")

    def test_the_record_keeps_the_rest_of_its_file(self):
        before = ("# kept\n[Other]\nCurrent=x\nName=y\n\n[Preset]\nCurrent=nebula\nNote=kept too\n\n"
                  "[Last]\nKey=value\n")
        (self.config / "boswasrc").write_text(before, encoding="utf-8")
        self.assertEqual(self.run_cli("apply", "ocean")[0], 0)
        self.assertEqual((self.config / "boswasrc").read_text(encoding="utf-8"),
                         before.replace("Current=nebula", "Current=ocean"))
        self.assertEqual(self.run_cli("reset", env=self.schemes(BoswasPrism=PRISM_SCHEME))[0], 0)
        self.assertEqual((self.config / "boswasrc").read_text(encoding="utf-8"),
                         before.replace("Current=nebula\n", ""))
        # A file that never held a preset is, after apply and reset, what it was.
        (self.config / "boswasrc").write_text("[Other]\nKeep=1\n", encoding="utf-8")
        self.assertEqual(self.run_cli("apply", "ocean")[0], 0)
        self.assertEqual((self.config / "boswasrc").read_text(encoding="utf-8"),
                         "[Other]\nKeep=1\n\n[Preset]\nCurrent=ocean\n")
        self.assertEqual(self.run_cli("reset", env=self.schemes(BoswasPrism=PRISM_SCHEME))[0], 0)
        self.assertEqual((self.config / "boswasrc").read_text(encoding="utf-8"), "[Other]\nKeep=1\n")

    def test_a_missing_scheme_stops_before_anything_is_written(self):
        (self.config / "kdeglobals").write_text(OLD_KDEGLOBALS, encoding="utf-8")
        before = self.tree()
        env = dict(self.env, BOSWAS_COLOR_SCHEMES=str(self.home / "none"))
        code, out, err = self.run_cli("--json", "apply", "midnight", env=env)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"error": json.loads(out)["error"], "code": 1, "step": "colours"})
        self.assertIn("step 'colours' failed", err)
        self.assertIn("BoswasMidnight.colors", err)
        self.assertEqual(self.tree(), before, "no file was created or changed")
        self.assertEqual(self.run_cli("--json", "reset", env=env)[0], 1)
        self.assertEqual(self.tree(), before)

    def test_a_symbolic_link_at_a_target_is_refused(self):
        """Neither read nor written through, nor replaced: the command stops and changes nothing."""
        elsewhere = self.home / "elsewhere"
        elsewhere.write_text("[Private]\nkey=value\n", encoding="utf-8")
        env = self.schemes(BoswasPearl=PEARL_SCHEME, BoswasPrism=PRISM_SCHEME)
        for name, step in (("boswas/appearance.toml", "appearance"), ("kdeglobals", "colours"),
                           ("boswasrc", "record")):
            for target in (elsewhere, self.home / "dangling"):
                for command in (("apply", "pearl"), ("reset",)):
                    with self.subTest(file=name, target=target.name, command=command[0]):
                        with tempfile.TemporaryDirectory(dir=self.home) as config:
                            self.config = Path(config)
                            link = self.config / name
                            link.parent.mkdir(exist_ok=True)
                            os.symlink(target, link)
                            for other in ("kdeglobals", "boswasrc"):
                                if other != name:
                                    (self.config / other).write_text("[Preset]\nCurrent=ember\n", encoding="utf-8")
                            before = self.tree()
                            code, out, err = self.run_cli("--json", *command,
                                                          env=dict(env, XDG_CONFIG_HOME=config))
                            self.assertEqual(code, 1)
                            self.assertEqual(json.loads(out)["step"], step)
                            self.assertIn(f"{link} is a symbolic link; refusing to follow it", err)
                            self.assertEqual(self.tree(), before)
                            self.assertEqual(elsewhere.read_text(encoding="utf-8"), "[Private]\nkey=value\n")
                            self.assertFalse((self.home / "dangling").exists())

    def test_a_linked_settings_folder_and_other_odd_targets_are_refused(self):
        env = self.schemes(BoswasPearl=PEARL_SCHEME, BoswasPrism=PRISM_SCHEME)
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.config / "boswas")
        for command in (("apply", "pearl"), ("reset",)):
            code, _out, err = self.run_cli(*command, env=env)
            self.assertEqual(code, 1)
            self.assertIn(f"step 'appearance' failed: {self.config / 'boswas'} is a symbolic link", err)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(sorted(self.tree()), ["boswas"])
        os.unlink(self.config / "boswas")
        for name, step in (("boswas", "appearance"), ("kdeglobals", "colours"), ("boswasrc", "record")):
            with self.subTest(name=name):
                path = self.config / name
                if name == "boswas":
                    path.write_text("a file where the folder should be", encoding="utf-8")
                    expected = "is not a folder"
                else:
                    path.mkdir()
                    expected = "is not a regular file"
                code, out, err = self.run_cli("--json", "apply", "pearl", env=env)
                self.assertEqual((code, json.loads(out)["step"]), (1, step))
                self.assertIn(expected, err)
                self.assertEqual(sorted(self.tree()), [name])
                path.unlink() if path.is_file() else path.rmdir()
        (self.config / "kdeglobals").write_bytes(b"x" * (cli.MAX_FILE + 1))
        code, _out, err = self.run_cli("apply", "pearl", env=env)
        self.assertEqual(code, 1)
        self.assertIn("is larger than 1024 KiB", err)
        self.assertEqual(sorted(self.tree()), ["kdeglobals"])

    def test_a_failed_write_leaves_the_old_file_and_no_temporary_file(self):
        env = self.schemes(BoswasPearl=PEARL_SCHEME)
        (self.config / "kdeglobals").write_text(OLD_KDEGLOBALS, encoding="utf-8")
        real = os.replace

        def failing(source, destination, **kwargs):
            if destination == "kdeglobals":
                raise OSError(28, "No space left on device")
            return real(source, destination, **kwargs)

        with mock.patch.object(cli.os, "replace", failing):
            code, out, err = self.run_cli("--json", "apply", "pearl", env=env)
        self.assertEqual((code, json.loads(out)["step"]), (1, "colours"))
        self.assertIn("No space left on device", err)
        self.assertEqual((self.config / "kdeglobals").read_text(encoding="utf-8"), OLD_KDEGLOBALS)
        self.assertEqual(sorted(self.tree()), ["boswas", "boswas/appearance.toml", "kdeglobals"],
                         "the steps before it were done, the one after it was not, no temporary file stays")

    def test_reset_returns_to_prism(self):
        env = self.schemes(BoswasPearl=PEARL_SCHEME, BoswasPrism=PRISM_SCHEME)
        (self.config / "kdeglobals").write_text(OLD_KDEGLOBALS, encoding="utf-8")
        self.assertEqual(self.run_cli("apply", "pearl", env=env)[0], 0)
        (self.config / "boswas" / "compositor.toml").write_text('[keyboard]\nlayout = "in"\n', encoding="utf-8")
        code, out, err = self.run_cli("--json", "reset", env=env)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"reset": True, "default": "Boswas Prism"})
        self.assertFalse((self.config / "boswas" / "appearance.toml").exists(), "the preset file is gone")
        self.assertTrue((self.config / "boswas" / "compositor.toml").exists(), "the user's own settings are kept")
        merged = support.parse_ini(self.config / "kdeglobals")
        self.assertEqual(merged["General"]["ColorScheme"], "BoswasPrism")
        self.assertEqual(merged["General"]["font"], "Inter,10")
        self.assertEqual(merged["Colors:Window"]["BackgroundNormal"], "18,16,28")
        self.assertFalse((self.config / "boswasrc").exists(), "a record that held nothing else is removed")
        self.assertEqual(self.run_cli("--json", "current", env=env)[1], '{\n  "current": null\n}\n')
        # A second reset, with nothing to remove, works the same; so does one in an empty home.
        code, out, err = self.run_cli("reset", env=env)
        self.assertEqual((code, out, err), (0, "Reset to Boswas Prism.\n", ""))
        with tempfile.TemporaryDirectory(dir=self.home) as config:
            self.assertEqual(self.run_cli("reset", env=dict(env, XDG_CONFIG_HOME=str(Path(config) / "new")))[0], 0)
            self.assertEqual(sorted(p.name for p in (Path(config) / "new").iterdir()), ["kdeglobals"])

    def test_reset_is_refused_for_root_and_takes_no_arguments(self):
        code, _out, err = self.run_cli("reset", euid=0)
        self.assertEqual(code, 4)
        code, _out, err = self.run_cli("reset", "pearl")
        self.assertEqual(code, 3)
        self.assertEqual(self.tree(), {})

    def test_refuses_root(self):
        code, out, err = self.run_cli("apply", "horizon", euid=0)
        self.assertEqual(code, 4)
        self.assertIn("root", err)
        self.assertEqual(self.tree(), {})

    def test_unknown_preset(self):
        code, out, err = self.run_cli("--json", "apply", "teal")
        self.assertEqual(code, 2)
        self.assertIn("unknown preset 'teal'", err)
        self.assertEqual(json.loads(out)["code"], 2)
        self.assertEqual(self.tree(), {})

    def test_usage_errors(self):
        for argv in ([], ["apply"], ["bogus"], ["list", "--nope"], ["apply", "ocean", "extra"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 3)
                self.assertEqual(self.tree(), {})


class ReloadTest(CliTestCase):
    def test_without_a_compositor(self):
        self.assertFalse(cli.reload_compositor({}))
        self.assertFalse(cli.reload_compositor({"XDG_RUNTIME_DIR": "/nonexistent", "WAYLAND_DISPLAY": "wayland-1"}))
        self.assertFalse(cli.reload_compositor({"XDG_RUNTIME_DIR": "/tmp", "WAYLAND_DISPLAY": "../etc/x"}))

    def test_apply_asks_the_running_compositor_to_reload(self):
        # A socket path is short: the stand-in for the compositor listens below /tmp.
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="bwp") as runtime:
            received = []
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(os.path.join(runtime, "boswas-wayland-7.sock"))
            server.listen(1)
            server.settimeout(10)

            def serve():
                connection, _ = server.accept()
                with connection:
                    received.append(connection.recv(4096))
                    connection.sendall(b'{"ok": true}\n')

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            env = dict(self.env, XDG_RUNTIME_DIR=runtime, WAYLAND_DISPLAY="wayland-7")
            try:
                self.assertEqual(self.run_cli("apply", "ocean", env=env)[0], 0)
                thread.join(10)
            finally:
                server.close()
            self.assertEqual(received, [b'{"cmd": "reload-config"}\n'])


class KConfigTextTest(unittest.TestCase):
    def test_setting_a_key(self):
        edit = cli.edit_kconfig
        self.assertEqual(edit("", "Preset", "Current", "ocean"), "[Preset]\nCurrent=ocean\n")
        self.assertEqual(edit("[A]\nx=1", "Preset", "Current", "ocean"), "[A]\nx=1\n\n[Preset]\nCurrent=ocean\n")
        self.assertEqual(edit("# note\n", "Preset", "Current", "ocean"), "# note\n\n[Preset]\nCurrent=ocean\n")
        self.assertEqual(edit("[Preset]\nOther=1\n\n[A]\nx=1\n", "Preset", "Current", "ocean"),
                         "[Preset]\nOther=1\nCurrent=ocean\n\n[A]\nx=1\n")
        self.assertEqual(edit(" [Preset] \n# why\nCurrent[$i]=a\nOther=1\n", "Preset", "Current", "ocean"),
                         " [Preset] \n# why\nCurrent=ocean\nOther=1\n", "in the place of the old value")
        self.assertEqual(edit("[A]\nx=1\n[Preset]\nCurrent=a\nCurrent=b\n[Preset]\nCurrent=c\n", "Preset", "Current",
                              "ocean"), "[A]\nx=1\n[Preset]\nCurrent=ocean\n", "written once")
        self.assertEqual(edit("[Preset]\nCurrently=1\nCurrentX=2\n", "Preset", "Current", "ocean"),
                         "[Preset]\nCurrently=1\nCurrentX=2\nCurrent=ocean\n", "other keys are other keys")

    def test_removing_a_key(self):
        edit = cli.edit_kconfig
        self.assertEqual(edit("[Preset]\nCurrent=nebula\n", "Preset", "Current", None), "")
        self.assertEqual(edit("", "Preset", "Current", None), "")
        self.assertEqual(edit("[A]\nCurrent=x\n\n[Preset]\nCurrent=a\n\n[Z]\ny=2\n", "Preset", "Current", None),
                         "[A]\nCurrent=x\n\n[Z]\ny=2\n")
        self.assertEqual(edit("[Preset]\nOther=1\nCurrent=a\n", "Preset", "Current", None), "[Preset]\nOther=1\n")
        for before in ("[Other]\nKeep=1\n", "# note\n", "[A]\nx=1\n\n[Z]\ny=2\n", ""):
            with self.subTest(before=before):                       # setting and removing leaves no trace
                self.assertEqual(edit(edit(before, "Preset", "Current", "ocean"), "Preset", "Current", None), before)
        unchanged = "# note\n[A]\nx=1\n"
        self.assertEqual(edit(unchanged, "Preset", "Current", None), unchanged)

    def test_merge_recognises_headers_with_blanks_around_them(self):
        merged = cli.merge_colors("  [Colors:Window]\t\nBackgroundNormal=0,0,0\n[General]\nColorScheme[$i]=Old\n",
                                  "[Colors:Window]\nBackgroundNormal=9,9,9\n", "BoswasX")
        self.assertEqual(merged, "[General]\nColorScheme=BoswasX\n\n[Colors:Window]\nBackgroundNormal=9,9,9\n")


class NoOtherProgramTest(unittest.TestCase):
    """boswas-preset writes the user's files itself and starts nothing."""

    def test_the_tool_starts_no_program_and_names_no_kde_program(self):
        for path in (support.PRESETS_DIR / "boswas_preset" / "cli.py", support.PRESETS_DIR / "boswas_preset" / "__init__.py",
                     support.PRESETS_DIR / "bin" / "boswas-preset"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                for word in ("subprocess", "os.system", "os.popen", "os.exec", "os.spawn", "os.posix_spawn", "Popen"):
                    self.assertNotIn(word, text)
                for program in ("kwriteconfig", "kreadconfig", "plasma", "konsole", "kscreenlocker", "qdbus",
                                "lookandfeel", "systemsettings", "kcmshell", "dolphin"):
                    self.assertNotIn(program, text.lower())


class LauncherTest(unittest.TestCase):
    def test_launcher(self):
        text = (support.PRESETS_DIR / "bin" / "boswas-preset").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/usr/bin/python3\n"))
        self.assertIn('sys.path.insert(0, "/usr/lib/boswas/python")', text)
        self.assertIn("from boswas_preset.cli import main", text)


if __name__ == "__main__":
    unittest.main()
