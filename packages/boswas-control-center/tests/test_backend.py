"""Backend, probes and the command runner (no Qt).

The Backend runs against a fake root directory, a recording command runner
and fake agent sockets (cc_fixtures.Rig); the transport is also exercised
against a real localapi server on a temporary socket, and the real
CommandRunner against the Python interpreter as a stand-in program.

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402

from boswas_agent.localapi import ApiError, ApiServer, Operation  # noqa: E402

from boswas_control_center import __version__, app, commands, probes  # noqa: E402
from boswas_control_center import viewmodel as vm  # noqa: E402
from boswas_control_center.backend import Backend  # noqa: E402
from boswas_control_center.catalog import (BY_KEY, COMPAT_MANAGER, FILES, PAGE_KEYS, PROGRAM_IDS,  # noqa: E402
                                           PROGRAMS, Program)
from boswas_control_center.errors import (CommandFailed, CommandUnavailable, ServiceRefused,  # noqa: E402
                                          ServiceUnavailable, user_message)

E = commands.EXECUTABLES


class RigTestCase(unittest.TestCase):
    live = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rig = fx.Rig(Path(self.tmp.name), live=self.live)
        self.backend = self.rig.backend

    def tearDown(self):
        self.tmp.cleanup()


class BackendReadTest(RigTestCase):
    def test_security_status(self):
        doc = self.backend.security_status()
        self.assertEqual(doc["compliance"]["state"], "COMPLIANT_WITH_WARNINGS")
        self.assertEqual(self.rig.commands.ran, [[E["boswas"], "--json", "status"]])
        self.assertEqual(self.rig.commands.timeouts, [60.0])

    def test_security_status_exit_one_is_a_result(self):
        self.rig.commands.outputs[(E["boswas"], "--json", "status")] = fx.completed(json.dumps(fx.STATUS), 1)
        self.assertEqual(len(self.backend.security_status()["checks"]), len(fx.STATUS["checks"]))

    def test_security_status_failures(self):
        key = (E["boswas"], "--json", "status")
        self.rig.commands.outputs[key] = fx.completed("", 70, "boswas: internal error: boom")
        with self.assertRaises(CommandFailed) as raised:
            self.backend.security_status()
        self.assertIn("boom", raised.exception.message)
        self.rig.commands.outputs[key] = fx.completed("not json")
        with self.assertRaises(CommandFailed):
            self.backend.security_status()
        self.rig.commands.outputs[key] = fx.completed(json.dumps({"checks": "x"}))
        with self.assertRaises(CommandFailed):
            self.backend.security_status()
        self.rig.commands.installed.discard(E["boswas"])
        with self.assertRaises(CommandUnavailable):
            self.backend.security_status()

    def test_presets(self):
        presets = self.backend.presets()
        self.assertEqual(presets.current, "boswas-dark")
        self.assertEqual(self.rig.commands.ran, [[E["boswas-preset"], "--json", "list"]])
        self.rig.commands.installed.discard(E["boswas-preset"])
        with self.assertRaises(CommandUnavailable) as raised:
            self.backend.presets()
        self.assertIn("not available", raised.exception.message)

    def test_presets_bad_output(self):
        self.rig.commands.outputs[(E["boswas-preset"], "--json", "list")] = fx.completed('{"presets": 3}')
        with self.assertRaises(CommandFailed):
            self.backend.presets()

    def test_agents(self):
        self.assertEqual(self.backend.agent_status()["state"], "READY")
        self.assertEqual(self.backend.agent_runtime()["apparmor"]["mode"], "enforce")
        self.assertEqual(self.backend.agent_config()["settings"]["INVENTORY_POLICY"], "standard")
        self.assertEqual(len(self.backend.windows_apps()), 2)
        self.assertTrue(self.backend.session_system()["runtime"]["healthy"])
        self.assertEqual(sorted(set(self.rig.services.calls)),
                         [("agent", "agent.status"), ("agent", "device.config"), ("agent", "runtime.status"),
                          ("session", "apps.list"), ("session", "system.status")])

    def test_agent_down(self):
        self.rig.services.agent_down = True
        with self.assertRaises(ServiceUnavailable) as raised:
            self.backend.agent_status()
        self.assertIn("device agent is not running", raised.exception.message)
        config = self.backend.agent_config()           # falls back to the allowlisted keys of device.conf
        self.assertEqual(config["source"], "file")
        self.assertEqual(config["settings"], {"INVENTORY_POLICY": "minimal", "TELEMETRY_POLICY": "none",
                                              "UPDATE_POLICY": "security-only"})

    def test_refused(self):
        self.rig.services.session["apps.list"] = ApiError("FORBIDDEN", "not yours")
        with self.assertRaises(ServiceRefused) as raised:
            self.backend.windows_apps()
        self.assertEqual(raised.exception.code, "FORBIDDEN")

    def test_only_read_only_operations(self):
        for op in ("apps.install", "apps.launch", "enroll", "maintenance.set"):
            with self.assertRaises(ValueError):
                self.backend._session_call(op)
            with self.assertRaises(ValueError):
                self.backend._agent_call(op)

    def test_release_hardware_storage(self):
        info = vm.release_info(self.backend.release())
        self.assertEqual(info.version_text, "v1 Alpha (1.0~alpha3)")
        self.assertEqual(info.debian_version, "13.7")
        hardware = self.backend.hardware()
        self.assertEqual(hardware["cpu_model"], "Intel(R) Core(TM) i7-1265U")
        self.assertEqual(hardware["cpu_threads"], 2)
        self.assertEqual(hardware["memory_bytes"], 16303520 * 1024)
        self.assertEqual(hardware["boot_mode"], "BIOS")
        storage = self.backend.storage()
        self.assertEqual([e["key"] for e in storage], ["system"])      # /home is on the same file system
        self.assertGreater(storage[0]["total"], 0)

    def test_power_never_reads_serial_numbers(self):
        supplies = self.backend.power()
        battery = next(s for s in supplies if s["name"] == "BAT0")
        self.assertEqual(battery["capacity"], "76")
        self.assertNotIn("serial_number", battery)
        self.assertNotIn("SECRET-SERIAL", json.dumps(supplies))

    def test_updates(self):
        doc = self.backend.updates()
        self.assertEqual(doc["source"], "apt-config")
        self.assertEqual(doc["periodic"]["Unattended-Upgrade"], "1")
        self.assertEqual(doc["stamps"], {"update-success-stamp": fx.STAMP_TIME})
        self.assertTrue(doc["log"]["readable"])
        self.assertEqual(doc["channel"], "dev")
        self.assertEqual(self.rig.commands.ran_program("apt-config")[0][:2], [E["apt-config"], "shell"])

    def test_updates_without_apt_config(self):
        self.rig.commands.installed.discard(E["apt-config"])
        doc = self.backend.updates()
        self.assertEqual(doc["source"], "files")
        self.assertEqual(doc["periodic"], {"Update-Package-Lists": "1", "Unattended-Upgrade": "1"})

    def test_desktop_is_the_native_desktop(self):
        self.assertEqual(self.backend.desktop(), "Boswas Desktop 1.0~alpha3")
        fx.write(self.rig.root / "usr/share/boswas/compositor/version", "not a version\n")
        self.assertIsNone(self.backend.desktop())
        os.remove(self.rig.root / "usr/share/boswas/compositor/version")
        self.assertIsNone(self.backend.desktop())
        self.assertEqual(self.rig.commands.ran, [])                 # read from a file: no program is asked

    def test_live_session(self):
        self.assertFalse(self.backend.live_session())
        fx.write(self.rig.root / "run/boswas/session", "live\n")
        self.assertTrue(self.backend.live_session())

    def test_program_availability(self):
        programs = self.backend.program_availability()
        self.assertEqual({key for key in programs if key in PROGRAM_IDS}, {"files", "compat-manager"})
        self.assertTrue(programs["compat-manager"])
        self.assertTrue(programs["files"])
        self.assertTrue(programs["gio"])
        self.rig.commands.installed.discard(E["boswas-files"])
        self.assertFalse(self.backend.program_availability()["files"])
        self.assertFalse(hasattr(self.backend, "kcm_availability"))

    def test_desktop_entries(self):
        entries = {e.desktop_id: e for e in self.backend.desktop_entries()}
        # OnlyShowIn / NotShowIn are read for the Boswas desktop: its own entries, none of another desktop's.
        self.assertEqual(set(entries), {"org.kde.kate", "org.kde.dolphin", "firefox-esr", "boswas-only",
                                        "com.boswas.CompatibilityManager", "com.boswas.ControlCenter",
                                        "kde4-oldapp", "boswas-winapp-com.example.notepad"})
        self.assertEqual(vm.CURRENT_DESKTOP, "Boswas")
        self.assertEqual(entries["firefox-esr"].name, "Firefox (Custom)")     # the user's entry wins
        self.assertEqual(entries["boswas-winapp-com.example.notepad"].winapp_id, "com.example.notepad")


class LiveRigTest(RigTestCase):
    live = True

    def test_live(self):
        self.assertTrue(self.backend.live_session())
        self.assertTrue(vm.updates_view(self.backend.updates(), live=True).log_note.endswith("shut down."))


class BackendActionTest(RigTestCase):
    def test_open_programs(self):
        self.backend.open_program(FILES)
        self.backend.open_program(COMPAT_MANAGER)
        self.assertEqual(self.rig.commands.started, [[E["boswas-files"], "/"], [E["boswas-compat-manager"]]])

    def test_every_page_program_has_an_exact_command(self):
        found = 0
        for key in PAGE_KEYS:
            for program in BY_KEY[key].programs:
                found += 1
                command = Backend.program_command(program)
                self.assertIn(command[0], commands.ALLOWED_PATHS)
                name, args = PROGRAMS[program.id]
                self.assertEqual(command, [E[name], *args])
        self.assertEqual(found, 2)

    def test_unknown_programs_are_refused(self):
        for program in (Program("kcm_evil", "x", "x", "x"), Program("--list", "x", "x", "x"),
                        Program("bash", "x", "x", "x"), Program("gio", "x", "x", "x"), "files", None):
            with self.assertRaises(ValueError):
                Backend.program_command(program)
        self.assertEqual(self.rig.commands.started, [])

    def test_missing_program(self):
        self.rig.commands.installed.discard(E["boswas-files"])
        with self.assertRaises(CommandUnavailable):
            self.backend.open_program(FILES)

    def test_launch(self):
        kate = vm.AppItem("desktop:org.kde.kate", vm.NATIVE, "Kate", "", "", "kate", "org.kde.kate",
                          "/usr/share/applications/org.kde.kate.desktop")
        self.backend.launch(kate)
        self.assertEqual(self.rig.commands.started[-1],
                         [E["gio"], "launch", "/usr/share/applications/org.kde.kate.desktop"])
        for path in ("relative.desktop", "/usr/share/applications/../../tmp/x.desktop", "/tmp/x.sh", ""):
            with self.assertRaises(ValueError, msg=path):
                self.backend.launch(vm.AppItem("desktop:x", vm.NATIVE, "x", "", "", "", "x", path))
        self.rig.commands.installed.discard(E["gio"])
        with self.assertRaises(CommandUnavailable) as caught:
            self.backend.launch(kate)
        self.assertIn("gio", user_message(caught.exception))

    def test_windows_applications_are_never_launched(self):
        item = vm.windows_item(fx.WINDOWS_APPS[0])
        with self.assertRaises(ValueError):
            self.backend.launch(item)
        bad = vm.AppItem("desktop:-x", vm.NATIVE, "x", "", "", "", "--help", "/x.desktop")
        with self.assertRaises(ValueError):
            self.backend.launch(bad)
        self.assertEqual(self.rig.commands.started, [])

    def test_apply_preset(self):
        self.backend.apply_preset("boswas-light")
        self.assertEqual(self.rig.commands.ran_program("boswas-preset"),
                         [[E["boswas-preset"], "apply", "boswas-light"]])
        self.assertEqual(self.rig.commands.timeouts[-1], 180.0)
        for bad in ("../x", "--layout", "x;rm", "", "ocean --layout", None):
            with self.assertRaises(ValueError):
                self.backend.apply_preset(bad)
        self.assertEqual(len(self.rig.commands.ran_program("boswas-preset")), 1)

    def test_a_preset_command_is_the_id_and_nothing_else(self):
        """The panel layout option left with Plasma: there is no second argument to pass."""
        self.assertEqual(self.backend.preset_command("classic"), [E["boswas-preset"], "apply", "classic"])
        with self.assertRaises(TypeError):
            self.backend.preset_command("classic", True)
        with self.assertRaises(TypeError):
            self.backend.apply_preset("classic", layout=True)
        self.assertFalse(hasattr(self.backend, "native_desktop"))

    def test_reset_look(self):
        # The same command whatever the session calls itself: there is one desktop.
        for desktop in ("Boswas", "KDE", ""):
            self.backend.environ["XDG_CURRENT_DESKTOP"] = desktop
            self.backend.reset_look()
            self.assertEqual(self.rig.commands.ran_program("boswas-preset")[-1], [E["boswas-preset"], "reset"])
        self.assertEqual(self.rig.commands.timeouts[-1], 180.0)
        self.rig.commands.outputs[(E["boswas-preset"], "reset")] = fx.completed("", 1, "step 'colours' failed")
        with self.assertRaises(CommandFailed) as raised:
            self.backend.reset_look()
        self.assertIn("colours", raised.exception.message)

    def test_apply_preset_failure(self):
        self.rig.commands.outputs[(E["boswas-preset"], "apply", "classic")] = fx.completed("", 3, "no such preset")
        with self.assertRaises(CommandFailed) as raised:
            self.backend.apply_preset("classic")
        self.assertIn("no such preset", raised.exception.message)

    def test_restart(self):
        self.backend.restart()
        self.assertEqual(self.rig.commands.ran_program("systemctl"), [[E["systemctl"], "reboot"]])
        self.rig.commands.outputs[(E["systemctl"], "reboot")] = fx.completed("", 1, "Access denied")
        with self.assertRaises(CommandFailed):
            self.backend.restart()


class TransportTest(unittest.TestCase):
    """The Backend against a real localapi server (the agent's own framing and errors)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "agent.sock"

        def status(_req):
            return dict(fx.AGENT)

        def refuse(_req):
            raise ApiError("FORBIDDEN", "runtime.status is not permitted")

        self.server = ApiServer(self.path, {"agent.status": Operation("agent.status", status),
                                            "runtime.status": Operation("runtime.status", refuse)})
        self.server.start()
        self.backend = Backend(root=self.tmp.name, environ={"XDG_RUNTIME_DIR": self.tmp.name},
                               commands_runner=fx.FakeCommands(Path(self.tmp.name)), agent_socket=str(self.path))

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def test_real_socket(self):
        self.assertEqual(self.backend.agent_status()["device_id"], fx.AGENT["device_id"])
        with self.assertRaises(ServiceRefused) as raised:
            self.backend.agent_runtime()
        self.assertEqual(raised.exception.code, "FORBIDDEN")
        with self.assertRaises(ServiceRefused) as raised:
            self.backend.agent_config()                     # unknown operation on this server
        self.assertEqual(raised.exception.code, "UNKNOWN_OPERATION")
        with self.assertRaises(ServiceUnavailable):
            self.backend.windows_apps()                     # no session socket in the temporary runtime dir


class CommandRunnerTest(unittest.TestCase):
    """The real runner, with the Python interpreter standing in as an allowed program."""

    def setUp(self):
        self.patch = mock.patch.object(commands, "ALLOWED_PATHS", frozenset({sys.executable}))
        self.patch.start()
        self.runner = commands.CommandRunner({"PATH": "/usr/bin:/bin"})

    def tearDown(self):
        self.patch.stop()

    def test_run_captures_output_without_a_shell(self):
        result = self.runner.run([sys.executable, "-c", "import os,sys; print(os.environ['LC_ALL']); "
                                  "print(sys.argv[1:])", "a b; echo injected"], timeout=20)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.stdout.splitlines(), ["C.UTF-8", "['a b; echo injected']"])

    def test_timeout(self):
        start = time.monotonic()
        with self.assertRaises(CommandFailed) as raised:
            self.runner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("did not finish", raised.exception.message)

    def test_refuses_programs_outside_the_allowlist(self):
        for command in (["/bin/sh", "-c", "true"], ["sh"], "python3 -c 1", [], [sys.executable, 3]):
            with self.assertRaises(ValueError):
                self.runner.run(command, timeout=5)
            with self.assertRaises(ValueError):
                self.runner.start(command)
        with self.assertRaises(ValueError):
            self.runner.run([sys.executable, "-c", "1"], timeout=0)

    def test_start_reports_an_immediate_failure(self):
        with self.assertRaises(CommandFailed) as raised:
            self.runner.start([sys.executable, "-c", "raise SystemExit(3)"], settle=10)
        self.assertEqual(raised.exception.code, 3)
        self.runner.start([sys.executable, "-c", "pass"], settle=10)

    def test_start_leaves_a_running_program_alone(self):
        start = time.monotonic()
        self.runner.start([sys.executable, "-c", "import time; time.sleep(3)"], settle=0.3)
        self.assertLess(time.monotonic() - start, 2.5)

    def test_missing_program(self):
        with mock.patch.object(commands, "ALLOWED_PATHS", frozenset({"/nonexistent/program"})):
            with self.assertRaises(CommandUnavailable):
                self.runner.run(["/nonexistent/program"], timeout=5)

    def test_argv_builder(self):
        self.assertEqual(commands.argv("boswas-files", "/"), ["/usr/bin/boswas-files", "/"])
        for bad in (("bash",), ("boswas-files", ""), ("boswas-files", "a\nb"), ("boswas-files", "a\0b"),
                    ("kcmshell6", "kcm_users"), ("kstart", "--application", "x"), ("dolphin", "/")):
            with self.assertRaises(ValueError):
                commands.argv(*bad)


class ProbeTest(unittest.TestCase):
    def test_xdg_dirs(self):
        self.assertEqual(probes.xdg_data_dirs({"HOME": "/home/a"}),
                         ["/home/a/.local/share", "/usr/local/share", "/usr/share"])
        self.assertEqual(probes.xdg_data_dirs({"XDG_DATA_HOME": "rel", "XDG_DATA_DIRS": "/a:rel:/a:/b"}), ["/a", "/b"])

    def test_missing_files_give_empty_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = probes.Root(tmp)
            self.assertEqual(probes.release(root)["release"], {})
            self.assertEqual(probes.power_supplies(root), [])
            self.assertEqual(probes.apt_stamps(root), {})
            self.assertEqual(probes.unattended_log(root), {"readable": False, "lines": []})
            self.assertFalse(probes.live_session(root))
            self.assertIsNone(probes.boswas_desktop_version(root))


class EntryPointTest(unittest.TestCase):
    def test_refuses_root(self):
        stderr = io.StringIO()
        with mock.patch.object(os, "geteuid", return_value=0, create=True), contextlib.redirect_stderr(stderr):
            self.assertEqual(app.main(["boswas-control-center"]), 4)
        self.assertIn("never as root", stderr.getvalue())

    def test_version_and_help_need_no_display(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            app.main(["boswas-control-center", "--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"boswas-control-center {__version__}")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            app.main(["boswas-control-center", "--help"])
        for key in PAGE_KEYS:
            self.assertIn(key, out.getvalue())

    def test_unknown_page(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            app.main(["boswas-control-center", "--page", "registry"])
        self.assertEqual(raised.exception.code, 2)

    def test_section_option(self):
        parser = app._parser()
        args = parser.parse_args(["--page", "network", "--section", "Wi-Fi"])
        self.assertEqual((args.page, args.section), ("network", "wi-fi"))
        self.assertEqual(parser.parse_args(["--page", "display", "--section", "night light"]).section, "night-light")
        for bad in ("", "---", "x" * 80, "\u00e4\u00f6"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit, msg=bad):
                parser.parse_args(["--page", "network", "--section", bad])

    def test_requested_page_of_another_start(self):
        """Arguments handed over by a second start: tolerant, never an exit, nothing but a known page."""
        self.assertEqual(app.requested_page([]), (None, None))
        self.assertEqual(app.requested_page(["--page", "display"]), ("display", None))
        self.assertEqual(app.requested_page(["--page=network", "--section", "Wi-Fi"]), ("network", "wi-fi"))
        self.assertEqual(app.requested_page(["--section=night-light", "--page", "display"]),
                         ("display", "night-light"))
        self.assertEqual(app.requested_page(["--section", "wi-fi"]), (None, None))       # a section needs a page
        for arguments in (["--page"], ["--page", "registry"], ["--page", "../etc"], ["--page", ""], ["display"],
                          ["--pages", "display"], ["--page", None], [None, 7], ["--page=", "--section="]):
            self.assertEqual(app.requested_page(arguments), (None, None), arguments)
        self.assertEqual(app.requested_page(["--page", "display", "--section", "\0"]), ("display", None))
        self.assertEqual(app.requested_page(["--page", "sound", "--page", "power"]), ("power", None))

    def test_error_messages(self):
        self.assertEqual(user_message(CommandUnavailable("gio")), "gio is not installed on this system.")
        self.assertIn("RuntimeError", user_message(RuntimeError("x")))


if __name__ == "__main__":
    unittest.main()
