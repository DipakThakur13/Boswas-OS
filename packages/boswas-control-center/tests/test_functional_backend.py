"""The functional backend: updates, software, Android, hardware, storage, network, long operations (no Qt).

Runs the real Backend on cc_fixtures.Rig: canned program output in the
formats of the real programs, scripted JSON Lines for boswas-sysops and
boswas-android, and a fake root with sysfs devices.

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402

from boswas_control_center import commands, jobs, software  # noqa: E402
from boswas_control_center.errors import CommandFailed, CommandUnavailable, Failed  # noqa: E402

E = commands.EXECUTABLES


class RigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rig = fx.Rig(Path(self.tmp.name))
        self.backend = self.rig.backend
        self.commands = self.rig.commands

    def tearDown(self):
        self.tmp.cleanup()

    def job(self, spec: jobs.JobSpec, handle=None) -> tuple[jobs.Outcome, list[jobs.Event]]:
        events: list[jobs.Event] = []
        outcome = self.backend.run_job(spec, events.append, handle or commands.StreamHandle())
        return outcome, events


class ReadTest(RigCase):
    def test_pending_updates(self):
        plan = self.backend.upgradable()
        self.assertEqual(len(plan.upgrades), 4)
        self.assertEqual(self.commands.ran_program("apt-get"),
                         [[E["apt-get"], "-s", "-o", "Debug::NoLocking=1", "--with-new-pkgs", "upgrade"]])
        self.commands.outputs[(E["apt-get"], "-s", "-o", "Debug::NoLocking=1", "--with-new-pkgs", "upgrade")] = \
            fx.completed("", 100, "E: Could not open lock file")
        with self.assertRaises(CommandFailed):
            self.backend.upgradable()

    def test_packages(self):
        hits = self.backend.apt_search("vlc")
        self.assertEqual([(h.name, h.is_installed) for h in hits], [("vlc", False), ("vlc-bin", True)])
        details = self.backend.apt_details("vlc")
        self.assertEqual((details["Candidate"], details["Installed"], details["Source"]),
                         ("3.0.21-10", "", "Debian (signed repository)"))
        for bad in ("-o x", "vlc;id", ""):
            with self.assertRaises(ValueError):
                self.backend.apt_search(bad)
        with self.assertRaises(ValueError):
            self.backend.apt_details("../x")

    def test_flatpak(self):
        state = self.backend.flatpak()
        self.assertEqual([(r.name, r.installation) for r in state.remotes], [("flathub", "system")])
        self.assertEqual(state.shipped, ("flathub",))
        self.assertEqual([a.app_id for a in state.apps], ["org.gimp.GIMP"])
        self.assertEqual(self.backend.flatpak_search("vlc")[0].app_id, "org.videolan.VLC")

    def test_flatpak_install_adds_the_administrators_remote_for_the_user(self):
        state = self.backend.flatpak()
        add = (E["flatpak"], "remote-add", "--user", "--if-not-exists", "flathub",
               "/usr/share/boswas/sysops/flatpak-remotes/flathub.flatpakrepo")
        install = (E["flatpak"], "install", "--user", "--noninteractive", "-y", "flathub", "org.videolan.VLC")
        self.commands.outputs[add] = fx.completed()
        self.commands.outputs[install] = fx.completed()
        self.backend.flatpak_install("flathub", "org.videolan.VLC", state)
        self.assertEqual(self.commands.ran[-2:], [list(add), list(install)])
        with self.assertRaises(Failed):
            self.backend.flatpak_install("other", "org.videolan.VLC", state)     # not enabled by the administrator
        with self.assertRaises(ValueError):
            self.backend.flatpak_install("flathub", "--system", state)
        self.commands.outputs[(E["flatpak"], "uninstall", "--user", "--noninteractive", "-y", "org.gimp.GIMP")] = \
            fx.completed("", 1, "error: org.gimp.GIMP not installed")
        with self.assertRaises(CommandFailed):
            self.backend.flatpak_uninstall("org.gimp.GIMP")

    def test_android(self):
        status = self.backend.android_status()
        self.assertEqual(status["schema"], "boswas-android/1")
        self.assertEqual([a.package for a in self.backend.android_apps()], ["org.fdroid.fdroid"])
        self.commands.installed.discard(E["boswas-android"])
        with self.assertRaises(CommandUnavailable):
            self.backend.android_status()

    def test_android_errors(self):
        self.commands.outputs[(E["boswas-android"], "--json", "status")] = fx.completed("not json", 0)
        with self.assertRaises(CommandFailed):
            self.backend.android_status()
        self.commands.outputs[(E["boswas-android"], "--json", "status")] = fx.completed(
            json.dumps({"schema": "boswas-android/1", "ok": False, "error": "runtime broken"}), 1)
        with self.assertRaises(CommandFailed) as raised:
            self.backend.android_status()
        self.assertIn("Runtime broken", raised.exception.message)
        self.commands.outputs[(E["boswas-android"], "--json", "status")] = fx.completed(json.dumps(
            {"schema": "boswas-android/1", "error": {"code": "RUNTIME", "message": "waydroid is not working",
                                                     "problems": ["the container service failed"]}}), 3)
        with self.assertRaises(CommandFailed) as raised:
            self.backend.android_status()
        self.assertEqual(raised.exception.message, "Waydroid is not working. The container service failed")

    def test_apk_inspection_checks_the_file_first(self):
        apk = Path(self.tmp.name) / "notes.apk"
        with zipfile.ZipFile(apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00")
        self.commands.outputs[(E["boswas-android"], "--json", "apk", "inspect", str(apk))] = \
            fx.completed(json.dumps(fx.APK_DOC))
        view = self.backend.android_inspect(str(apk))
        self.assertEqual(view.package, "org.example.notes")
        exe = Path(self.tmp.name) / "notes2.apk"
        exe.write_bytes(b"MZ" + b"\0" * 64)
        with self.assertRaises(Failed):
            self.backend.android_inspect(str(exe))

    def test_hardware(self):
        pci = self.backend.pci()
        realtek = next(d for d in pci if d.vendor_id == "10ec")
        self.assertEqual((realtek.name, realtek.driver), ("RTL8125 2.5GbE Controller", ""))
        gpu = next(d for d in pci if d.category == "Graphics")
        self.assertEqual(gpu.driver, "i915")
        usb = self.backend.usb()
        self.assertEqual([(d.name, d.driver) for d in usb], [("USB Keyboard", "usbhid")])
        self.assertNotIn("SECRET-USB-SERIAL", repr(usb) + repr(pci))

    def test_storage(self):
        disks = self.backend.disks()
        self.assertEqual([d.name for d in disks], ["nvme0n1", "sda"])
        self.assertNotIn("SERIAL", " ".join(self.commands.ran_program("lsblk")[0]))
        self.assertEqual(self.backend.thumbnails(), (0, 0))

    def test_network(self):
        state = self.backend.network()
        wifi = next(d for d in state.devices if d.device == "wlp2s0")
        self.assertEqual(wifi.addresses, ("192.168.1.23/24",))
        self.assertTrue(state.wifi_enabled)
        # Only connected devices are asked for their addresses.
        shows = [c for c in self.commands.ran_program("nmcli") if "show" in c]
        self.assertEqual(len(shows), 1)
        self.backend.set_wifi(False)
        self.backend.device_connection("wlp2s0", False)
        self.assertIn([E["nmcli"], "device", "disconnect", "wlp2s0"], self.commands.ran)
        with self.assertRaises(ValueError):
            self.backend.device_connection("wlp2s0; reboot", True)

    def test_load(self):
        load = self.backend.load()
        self.assertIsNone(load["cpu_percent"])            # the fake /proc/stat does not change between samples
        self.assertEqual(load["memory"][0], 16303520 * 1024)

    def test_files(self):
        deb = Path(self.tmp.name) / "tool.deb"
        deb.write_bytes(b"!<arch>\ndebian-binary   1342943816  0     0     100644  4         `\n2.0\n")
        self.assertEqual(self.backend.detect_file(str(deb)).kind, "deb")
        exe = Path(self.tmp.name) / "setup.exe"
        exe.write_bytes(b"MZ" + b"\0" * 64)
        self.backend.open_windows_installer(str(exe))
        self.assertEqual(self.commands.started[-1], [E["boswas-compat-manager"], "--install", str(exe)])
        with self.assertRaises(Failed):
            self.backend.open_windows_installer(str(deb))
        with self.assertRaises(Failed):
            self.backend.detect_file("relative")

    def test_appimages(self):
        image = Path(self.tmp.name) / "Tool.AppImage"
        image.write_bytes(b"\x7fELF\x02\x01\x01\x00AI\x02" + b"\0" * 32)
        app = self.backend.add_appimage(str(image))
        self.assertEqual([a.ident for a in self.backend.appimages()], [app.ident])
        self.backend.remove_appimage(app.ident)
        self.assertEqual(self.backend.appimages(), [])
        not_image = Path(self.tmp.name) / "x.AppImage"
        not_image.write_bytes(b"#!/bin/sh\n")
        with self.assertRaises(Failed):
            self.backend.add_appimage(str(not_image))


class JobTest(RigCase):
    def sysops(self, *words: str) -> tuple:
        return tuple(commands.sysops_argv(*words))

    def test_success_with_progress(self):
        self.commands.streams[self.sysops("apt-upgrade")] = (fx.sysops_lines("apt-upgrade", message="Upgraded."), 0)
        outcome, events = self.job(jobs.JobSpec(jobs.SYSOPS, ("apt-upgrade",), "Installing updates"))
        self.assertTrue(outcome.ok)
        self.assertEqual([e.kind for e in events], ["start", "progress", "progress", "result"])
        self.assertEqual(self.commands.streamed, [list(self.sysops("apt-upgrade"))])

    def test_failures(self):
        cases = {
            "LOCKED": (fx.sysops_lines("apt-install", ok=False, code="LOCKED", message="dpkg is locked.",
                                       data={"holder": {"pid": 42, "command": "unattended-upgr"}}), 3, True),
            "NETWORK": (fx.sysops_lines("apt-install", ok=False, code="NETWORK", message="Download failed."), 5, True),
            "NOT_ALLOWED": (fx.sysops_lines("apt-install", ok=False, code="NOT_ALLOWED",
                                            message="openssh-server is excluded by Boswas policy."), 2, False),
        }
        for code, (lines, exit_code, retryable) in cases.items():
            self.commands.streams[self.sysops("apt-install", "vlc")] = (lines, exit_code)
            outcome, _events = self.job(jobs.JobSpec(jobs.SYSOPS, ("apt-install", "vlc"), "Installing vlc"))
            self.assertEqual((outcome.ok, outcome.code, outcome.retryable), (False, code, retryable), code)

    def test_authorization_denied(self):
        self.commands.streams[self.sysops("apt-refresh")] = (["Error executing command as another user: Request "
                                                              "dismissed"], 126)
        outcome, _ = self.job(jobs.JobSpec(jobs.SYSOPS, ("apt-refresh",), "Checking"))
        self.assertTrue(outcome.auth_denied)

    def test_no_result_and_garbage(self):
        self.commands.streams[self.sysops("apt-clean")] = (["{broken", "plain text"], 0)
        outcome, _ = self.job(jobs.JobSpec(jobs.SYSOPS, ("apt-clean",), "Cleaning"))
        self.assertEqual((outcome.ok, outcome.code), (False, jobs.MALFORMED))
        outcome, _ = self.job(jobs.JobSpec(jobs.SYSOPS, ("journal-vacuum",), "Logs"))     # no script: no output
        self.assertFalse(outcome.ok)

    def test_cancel_accepted_while_downloading(self):
        command = self.sysops("apt-refresh")
        started = threading.Event()

        def script(handle, emit):
            emit(json.dumps({"type": "start", "operation": "apt-refresh", "cancellable": True}))
            emit(json.dumps({"type": "progress", "phase": "refresh", "percent": 5, "cancellable": True}))
            started.set()
            deadline = time.monotonic() + 10
            while not handle.cancel_requested and time.monotonic() < deadline:
                time.sleep(0.01)
            emit(json.dumps({"type": "result", "ok": False, "code": "CANCELLED", "message": "Cancelled.",
                             "data": {}}))
            return 4

        self.commands.streams[command] = script
        handle = commands.StreamHandle()
        result: list = []
        worker = threading.Thread(target=lambda: result.append(self.job(
            jobs.JobSpec(jobs.SYSOPS, ("apt-refresh",), "Checking"), handle)))
        worker.start()
        self.assertTrue(started.wait(5))
        self.assertTrue(handle.cancel())
        worker.join(10)
        self.assertEqual(result[0][0].code, "CANCELLED")

    def test_cancel_refused_while_installing(self):
        """The helper decides: it logs that it cannot stop, finishes and reports the real result."""
        command = self.sysops("apt-upgrade")

        def script(handle, emit):
            emit(json.dumps({"type": "start", "operation": "apt-upgrade", "cancellable": True}))
            emit(json.dumps({"type": "progress", "phase": "install", "percent": 50, "cancellable": False}))
            handle.cancel()
            emit(json.dumps({"type": "log", "level": "warning",
                             "message": "Cannot cancel while packages are being installed."}))
            emit(json.dumps({"type": "result", "ok": True, "code": "OK", "message": "Upgraded.", "data": {}}))
            return 0

        self.commands.streams[command] = script
        outcome, events = self.job(jobs.JobSpec(jobs.SYSOPS, ("apt-upgrade",), "Installing"))
        self.assertTrue(outcome.ok)
        self.assertIn("warning", [e.level for e in events if e.kind == "log"])

    def test_android_jobs(self):
        command = tuple(commands.android_argv("runtime", "install"))
        self.commands.streams[command] = (fx.sysops_lines("android-runtime-install"), 0)
        outcome, _ = self.job(jobs.JobSpec(jobs.ANDROID, ("runtime", "install"), "Installing"))
        self.assertTrue(outcome.ok)
        self.commands.installed.discard(E["boswas-android"])
        with self.assertRaises(CommandUnavailable):
            self.job(jobs.JobSpec(jobs.ANDROID, ("runtime", "install"), "Installing"))

    def test_helper_missing(self):
        self.commands.installed.discard(E["pkexec"])
        with self.assertRaises(CommandUnavailable) as raised:
            self.job(jobs.JobSpec(jobs.SYSOPS, ("apt-refresh",), "Checking"))
        self.assertEqual(raised.exception.message, commands.NO_ADMIN)

    def test_invalid_specs_never_reach_a_process(self):
        for spec in (jobs.JobSpec(jobs.SYSOPS, ("apt-install", "-y"), "x"),
                     jobs.JobSpec(jobs.SYSOPS, ("rm",), "x"),
                     jobs.JobSpec(jobs.ANDROID, ("runtime", "init", "--gapps"), "x"),
                     jobs.JobSpec("shell", ("id",), "x")):
            with self.assertRaises(ValueError):
                self.job(spec)
        self.assertEqual(self.commands.streamed, [])


class SoftwareStateTest(unittest.TestCase):
    def test_plan_is_sorted_security_first(self):
        plan = software.parse_simulated_upgrade(fx.UPGRADE_SIM)
        self.assertEqual([u.security for u in plan.upgrades], [True, True, False, False])


if __name__ == "__main__":
    unittest.main()
