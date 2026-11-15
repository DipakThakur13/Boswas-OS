"""boswas-preset: list, current and apply (with a fake command runner)."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support

sys.path.insert(0, str(support.PRESETS_DIR))
from boswas_preset import cli  # noqa: E402


class FakeRunner:
    def __init__(self, fail_at=None, status=1):
        self.calls = []
        self.fail_at = fail_at
        self.status = status

    def __call__(self, argv):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv), argv
        self.calls.append(argv)
        if self.fail_at is not None and len(self.calls) - 1 == self.fail_at:
            return self.status
        return 0


def expected_steps(preset_id, label, layout=False):
    lnf = ["plasma-apply-lookandfeel", "-a", f"com.boswas.{preset_id}"] + (["--resetLayout"] if layout else [])
    return [
        lnf,
        ["plasma-apply-wallpaperimage", f"/usr/share/wallpapers/Boswas-{label}"],
        ["kwriteconfig6", "--file", "kscreenlockerrc", "--group", "Greeter", "--group", "Wallpaper",
         "--group", "org.kde.image", "--group", "General", "--key", "Image",
         f"file:///usr/share/boswas/presets/lock/{preset_id}.png"],
        ["kwriteconfig6", "--file", "konsolerc", "--group", "Desktop Entry", "--key", "DefaultProfile",
         f"Boswas {label}.profile"],
        ["kwriteconfig6", "--file", "boswasrc", "--group", "Preset", "--key", "Current", preset_id],
    ]


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.config = self.home / "config"
        self.config.mkdir()
        self.system = self.home / "xdg"
        self.system.mkdir()
        self.env = {
            "BOSWAS_PRESETS_FILE": str(support.PRESETS_FILE),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_CONFIG_DIRS": str(self.system),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv, runner=None, euid=1000, env=None):
        out, err = io.StringIO(), io.StringIO()
        runner = runner or FakeRunner()
        code = cli.main(list(argv), runner=runner, env=env or self.env, geteuid=lambda: euid,
                        stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue(), runner


class ListTest(CliTestCase):
    def test_json_list(self):
        code, out, err, runner = self.run_cli("--json", "list")
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
        self.assertEqual(runner.calls, [])

    def test_json_flag_after_the_command(self):
        code, out, _, _ = self.run_cli("list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["presets"]), 10)

    def test_text_list_marks_the_current_preset(self):
        (self.config / "boswasrc").write_text("[Preset]\nCurrent=ember\n", encoding="utf-8")
        code, out, _, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 10)
        self.assertTrue(lines[7].startswith("* ember"))

    def test_unreadable_presets_file(self):
        env = dict(self.env, BOSWAS_PRESETS_FILE=str(self.home / "missing.json"))
        code, out, err, _ = self.run_cli("--json", "list", env=env)
        self.assertEqual(code, 1)
        self.assertIn("missing.json", err)
        self.assertEqual(json.loads(out)["code"], 1)


class CurrentTest(CliTestCase):
    def test_nothing_recorded(self):
        code, out, _, _ = self.run_cli("current")
        self.assertEqual((code, out), (0, ""))
        code, out, _, _ = self.run_cli("--json", "current")
        self.assertEqual(json.loads(out), {"current": None})

    def test_recorded_by_apply(self):
        (self.config / "boswasrc").write_text("# comment\n[Other]\nCurrent=x\n\n[Preset]\nCurrent=nebula\n",
                                              encoding="utf-8")
        code, out, _, _ = self.run_cli("current")
        self.assertEqual((code, out), (0, "nebula\n"))
        code, out, _, _ = self.run_cli("current", "--json")
        self.assertEqual(json.loads(out), {"current": "nebula"})

    def test_unknown_recorded_id_is_ignored(self):
        (self.config / "boswasrc").write_text("[Preset]\nCurrent=bogus\n", encoding="utf-8")
        code, out, _, _ = self.run_cli("--json", "current")
        self.assertEqual(json.loads(out), {"current": None})

    def test_falls_back_to_the_active_global_theme(self):
        (self.system / "kdeglobals").write_text("[KDE]\nLookAndFeelPackage=com.boswas.horizon\n", encoding="utf-8")
        _, out, _, _ = self.run_cli("--json", "current")
        self.assertEqual(json.loads(out), {"current": "horizon"})
        # The user's own setting wins over the system default.
        (self.config / "kdeglobals").write_text("[KDE]\nLookAndFeelPackage=org.kde.breezedark.desktop\n",
                                                encoding="utf-8")
        _, out, _, _ = self.run_cli("--json", "current")
        self.assertEqual(json.loads(out), {"current": None})

    def test_default_config_home(self):
        env = {k: v for k, v in self.env.items() if k != "XDG_CONFIG_HOME"}
        (self.home / ".config").mkdir()
        (self.home / ".config" / "boswasrc").write_text("[Preset]\nCurrent=slate\n", encoding="utf-8")
        _, out, _, _ = self.run_cli("current", env=env)
        self.assertEqual(out, "slate\n")


class ApplyTest(CliTestCase):
    def test_apply_runs_the_steps_in_order(self):
        for preset in support.presets():
            with self.subTest(preset=preset["id"]):
                code, out, err, runner = self.run_cli("apply", preset["id"])
                self.assertEqual(code, 0, err)
                self.assertEqual(runner.calls, expected_steps(preset["id"], preset["label"]))
                self.assertIn(preset["name"], out)

    def test_layout_adds_reset_layout_only_to_the_global_theme_step(self):
        code, out, err, runner = self.run_cli("apply", "classic", "--layout", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(runner.calls, expected_steps("classic", "Classic", layout=True))
        self.assertEqual(sum(call.count("--resetLayout") for call in runner.calls), 1)
        self.assertEqual(json.loads(out), {"applied": "classic", "layout": True})

    def test_json_without_layout(self):
        code, out, _, runner = self.run_cli("--json", "apply", "horizon")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"applied": "horizon", "layout": False})
        self.assertNotIn("--resetLayout", runner.calls[0])

    def test_native_desktop_writes_appearance_and_colours(self):
        """On the native Boswas desktop no Plasma tool is used."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "config"
            schemes = Path(tmp) / "schemes"
            schemes.mkdir()
            (schemes / "BoswasPearl.colors").write_text(
                "[ColorEffects:Disabled]\nColor=1,2,3\n\n[Colors:Window]\nBackgroundNormal=250,250,250\n\n"
                "[General]\nName=Boswas Pearl\n\n[WM]\nactiveBackground=1,1,1\n", encoding="utf-8")
            home.mkdir()
            (home / "kdeglobals").write_text(
                "[General]\nColorScheme=Old\nfont=Inter,10\n\n[Colors:Window]\nBackgroundNormal=0,0,0\n\n"
                "[Icons]\nTheme=Boswas-Prism\n", encoding="utf-8")
            env = dict(self.env, XDG_CURRENT_DESKTOP="Boswas", XDG_CONFIG_HOME=str(home),
                       BOSWAS_COLOR_SCHEMES=str(schemes))
            code, out, err, runner = self.run_cli("--json", "apply", "pearl", "--layout", env=env)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out), {"applied": "pearl", "layout": False})
            self.assertEqual([call[0] for call in runner.calls], ["kwriteconfig6", "kwriteconfig6"])
            self.assertFalse(any("plasma" in part for call in runner.calls for part in call))
            self.assertEqual(runner.calls[-1][-1], "pearl")

            appearance = (home / "boswas" / "appearance.toml").read_text(encoding="utf-8")
            self.assertIn('scheme = "light"', appearance)
            self.assertIn('wallpaper = "/usr/share/wallpapers/Boswas-Pearl/contents/images/3840x2160.png"', appearance)
            import tomllib
            self.assertEqual(set(tomllib.loads(appearance)), {"appearance"})

            merged = support.parse_ini(home / "kdeglobals")
            self.assertEqual(merged["General"]["ColorScheme"], "BoswasPearl")
            self.assertEqual(merged["General"]["font"], "Inter,10", "other settings are kept")
            self.assertEqual(merged["Colors:Window"]["BackgroundNormal"], "250,250,250")
            self.assertEqual(merged["WM"]["activeBackground"], "1,1,1")
            self.assertEqual(merged["Icons"]["Theme"], "Boswas-Prism")
            self.assertNotIn("ColorEffects:Disabled", merged, "only colour groups are copied")
            self.assertFalse(list(home.rglob("*.tmp")), "no temporary file is left behind")

    def test_native_desktop_dark_preset_and_missing_scheme(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(self.env, XDG_CURRENT_DESKTOP="Boswas", XDG_CONFIG_HOME=tmp,
                       BOSWAS_COLOR_SCHEMES=str(Path(tmp) / "none"))
            code, out, err, runner = self.run_cli("--json", "apply", "midnight", env=env)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["step"], "colours")
            self.assertEqual(runner.calls, [], "nothing runs after a failed step")
            appearance = (Path(tmp) / "boswas" / "appearance.toml").read_text(encoding="utf-8")
            self.assertIn('scheme = "dark"', appearance)
            self.assertIn("contents/images_dark/3840x2160.png", appearance)

    def test_native_reset_returns_to_prism(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "config"
            schemes = Path(tmp) / "schemes"
            schemes.mkdir()
            (schemes / "BoswasPrism.colors").write_text(
                "[Colors:Window]\nBackgroundNormal=18,16,28\n\n[General]\nName=Boswas Prism\n\n"
                "[WM]\nactiveBackground=26,23,40\n", encoding="utf-8")
            (home / "boswas").mkdir(parents=True)
            (home / "boswas" / "appearance.toml").write_text('[appearance]\nscheme = "light"\n', encoding="utf-8")
            (home / "boswas" / "compositor.toml").write_text('[keyboard]\nlayout = "in"\n', encoding="utf-8")
            (home / "kdeglobals").write_text(
                "[General]\nColorScheme=BoswasPearl\nfont=Inter,10\n\n[Colors:Window]\nBackgroundNormal=250,250,250\n",
                encoding="utf-8")
            env = dict(self.env, XDG_CURRENT_DESKTOP="Boswas", XDG_CONFIG_HOME=str(home),
                       BOSWAS_COLOR_SCHEMES=str(schemes))
            code, out, err, runner = self.run_cli("--json", "reset", env=env)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out), {"reset": True, "default": "Boswas Prism"})
            self.assertFalse((home / "boswas" / "appearance.toml").exists(), "the preset file is gone")
            self.assertTrue((home / "boswas" / "compositor.toml").exists(), "the user's own settings are kept")
            merged = support.parse_ini(home / "kdeglobals")
            self.assertEqual(merged["General"]["ColorScheme"], "BoswasPrism")
            self.assertEqual(merged["General"]["font"], "Inter,10")
            self.assertEqual(merged["Colors:Window"]["BackgroundNormal"], "18,16,28")
            self.assertEqual(runner.calls, [
                ["kwriteconfig6", "--file", "konsolerc", "--group", "Desktop Entry", "--key", "DefaultProfile",
                 "Boswas Prism.profile"],
                ["kwriteconfig6", "--file", "boswasrc", "--group", "Preset", "--key", "Current", "--delete"],
            ])
            # A second reset, with nothing to remove, works the same.
            code, _out, err, _runner = self.run_cli("reset", env=env)
            self.assertEqual((code, err), (0, ""))

    def test_reset_on_plasma_applies_the_default_preset_with_its_layout(self):
        code, out, _, runner = self.run_cli("--json", "reset")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"reset": True, "default": "Boswas Horizon"})
        self.assertEqual(runner.calls, expected_steps("horizon", "Horizon", layout=True))

    def test_reset_is_refused_for_root_and_takes_no_arguments(self):
        code, _out, err, runner = self.run_cli("reset", euid=0)
        self.assertEqual(code, 4)
        self.assertEqual(runner.calls, [])
        code, _out, err, runner = self.run_cli("reset", "pearl")
        self.assertEqual(code, 3)
        self.assertEqual(runner.calls, [])

    def test_native_desktop_detection_and_reload_without_a_compositor(self):
        self.assertTrue(cli.is_native_desktop({"XDG_CURRENT_DESKTOP": "Boswas"}))
        self.assertTrue(cli.is_native_desktop({"XDG_CURRENT_DESKTOP": "Boswas:wlroots"}))
        for other in ("KDE", "", "NotBoswas", "boswas"):
            self.assertFalse(cli.is_native_desktop({"XDG_CURRENT_DESKTOP": other}), other)
        self.assertFalse(cli.is_native_desktop({}))
        self.assertFalse(cli.reload_compositor({}))
        self.assertFalse(cli.reload_compositor({"XDG_RUNTIME_DIR": "/nonexistent", "WAYLAND_DISPLAY": "wayland-1"}))
        self.assertFalse(cli.reload_compositor({"XDG_RUNTIME_DIR": "/tmp", "WAYLAND_DISPLAY": "../etc/x"}))

    def test_refuses_root(self):
        code, out, err, runner = self.run_cli("apply", "horizon", euid=0)
        self.assertEqual(code, 4)
        self.assertEqual(runner.calls, [])
        self.assertIn("root", err)

    def test_unknown_preset(self):
        code, out, err, runner = self.run_cli("--json", "apply", "teal")
        self.assertEqual(code, 2)
        self.assertEqual(runner.calls, [])
        self.assertIn("unknown preset 'teal'", err)
        self.assertEqual(json.loads(out)["code"], 2)

    def test_failing_step_stops_and_is_reported(self):
        runner = FakeRunner(fail_at=1, status=3)
        code, out, err, _ = self.run_cli("apply", "ocean", "--json", runner=runner)
        self.assertEqual(code, 1)
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("wallpaper", err)
        self.assertIn("status 3", err)
        self.assertEqual(json.loads(out), {"error": json.loads(out)["error"], "code": 1, "step": "wallpaper"})

    def test_usage_errors(self):
        for argv in ([], ["apply"], ["bogus"], ["list", "--nope"]):
            with self.subTest(argv=argv):
                code, _, err, runner = self.run_cli(*argv)
                self.assertEqual(code, 3)
                self.assertEqual(runner.calls, [])


class DefaultRunnerTest(unittest.TestCase):
    def test_never_uses_a_shell(self):
        completed = mock.Mock(returncode=5, stdout=b"")
        with mock.patch.object(cli.subprocess, "run", return_value=completed) as run:
            status = cli.default_runner(["kwriteconfig6", "--file", "x; rm -rf /"])
        self.assertEqual(status, 5)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["kwriteconfig6", "--file", "x; rm -rf /"])
        self.assertIs(kwargs["shell"], False)

    def test_missing_program(self):
        with mock.patch.object(cli.subprocess, "run", side_effect=FileNotFoundError()), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(cli.default_runner(["plasma-apply-lookandfeel"]), 127)


class LauncherTest(unittest.TestCase):
    def test_launcher(self):
        text = (support.PRESETS_DIR / "bin" / "boswas-preset").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/usr/bin/python3\n"))
        self.assertIn('sys.path.insert(0, "/usr/lib/boswas/python")', text)
        self.assertIn("from boswas_preset.cli import main", text)


if __name__ == "__main__":
    unittest.main()
