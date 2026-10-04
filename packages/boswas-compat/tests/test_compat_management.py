"""Unit tests for the WinCompat management operations added for the device
agent and the Compatibility Manager: inspect, stop, upgrade, logs --clear,
runtime, normalised application states, MSI architecture detection, the
fixed 32-bit refusal, device policy lists and the managed policy layer.

Run: python3 -m unittest discover -s tests
"""

import contextlib
import hashlib
import io
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1]))

from test_boswas_compat import COMPAT, CompatTestCase, manifest_doc, pe_bytes  # noqa: E402

from boswas_compat import UNSUPPORTED_32BIT_MESSAGE, cli, executor, installer, ops, policy, store  # noqa: E402
from boswas_compat.errors import Busy, Refused, WinAppError  # noqa: E402

ENDOFCHAIN, FREESECT, FATSECT = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD
LOCK_HOLDER = ("import fcntl, os, sys, time\n"
               "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
               "fcntl.flock(fd, fcntl.LOCK_EX)\n"
               "print('locked', flush=True)\n"
               "time.sleep(60)\n")


def start_process(test, argv, **kwargs):
    """A helper process that is always killed, reaped and closed after the test."""
    proc = subprocess.Popen(argv, **kwargs)

    def cleanup():
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
    test.addCleanup(cleanup)
    return proc


def property_stream(template: str) -> bytes:
    value = struct.pack("<HxxI", 0x1E, len(template) + 1) + template.encode("latin-1") + b"\0"
    value += b"\0" * (-len(value) % 4)
    body = struct.pack("<II", 7, 16) + value                       # one property: PID 7 at offset 16
    section = struct.pack("<II", 8 + len(body), 1) + body
    header = b"\xfe\xff" + struct.pack("<HI", 0, 0x00020006) + b"\0" * 16 + struct.pack("<I", 1)
    return header + b"\x01" * 16 + struct.pack("<I", 48) + section


def msi_bytes(template: str | None = "x64;1033", *, loop: bool = False, stream_name="\x05SummaryInformation") -> bytes:
    """A minimal OLE compound file with a SummaryInformation stream in the mini stream."""
    sector = 512
    stream = property_stream(template) if template is not None else b"\xfe\xff" + b"\0" * 46
    mini_count = -(-len(stream) // 64)
    ministream = stream + b"\0" * (mini_count * 64 - len(stream))
    big_count = -(-len(ministream) // sector)
    ministream += b"\0" * (big_count * sector - len(ministream))

    fat = [FATSECT, 1 if loop else ENDOFCHAIN, ENDOFCHAIN]
    fat += [3 + i + 1 for i in range(big_count - 1)] + [ENDOFCHAIN]
    fat += [FREESECT] * (128 - len(fat))
    mini_fat = [i + 1 for i in range(mini_count - 1)] + [ENDOFCHAIN]
    mini_fat += [FREESECT] * (128 - len(mini_fat))

    def entry(name: str, kind: int, start: int, size: int) -> bytes:
        raw = bytearray(128)
        encoded = (name + "\0").encode("utf-16-le")
        raw[:len(encoded)] = encoded
        struct.pack_into("<H", raw, 0x40, len(encoded))
        raw[0x42] = kind
        struct.pack_into("<III", raw, 0x44, FREESECT, FREESECT, 1 if kind == 5 else FREESECT)
        struct.pack_into("<I", raw, 0x74, start)
        struct.pack_into("<Q", raw, 0x78, size)
        return bytes(raw)

    directory = entry("Root Entry", 5, 3, mini_count * 64) + entry(stream_name, 2, 0, len(stream))
    directory += bytes(128) * 2
    header = bytearray(512)
    header[:8] = installer.OLE_MAGIC
    struct.pack_into("<HHHHH", header, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIII", header, 0x2C, 1, 1, 0, 4096, 2, 1, ENDOFCHAIN, 0)
    struct.pack_into("<109I", header, 0x4C, 0, *([FREESECT] * 108))
    return (bytes(header) + struct.pack("<128I", *fat) + directory + struct.pack("<128I", *mini_fat)
            + ministream)


class ArchitectureTests(CompatTestCase):
    def test_32_bit_refusal_uses_the_product_message_before_anything_else(self):
        self.set_policy(UNLISTED_APPS="deny")          # architecture is decided first
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(self.installer_file("setup32.exe", pe_bytes(machine=0x14C))))
        self.assertEqual(cm.exception.reason, "architecture")
        self.assertIn(UNSUPPORTED_32BIT_MESSAGE, str(cm.exception))
        for hint in ("wine32", "i386", "WoW64", "multiarch"):
            self.assertNotIn(hint.lower(), str(cm.exception).lower())
        self.assertEqual(self.executor.calls, [])

    def test_msi_platform_is_read_from_the_summary_information(self):
        for template, machine in (("x64;1033", "x86_64"), ("Intel;1033", "x86"), (";1033", "x86"),
                                  ("Arm64;1033", "arm64"), ("AMD64;0", "x86_64")):
            path = self.installer_file("p.msi", msi_bytes(template))
            self.assertEqual(installer.inspect(path).machine, machine, template)
            self.assertEqual(installer.inspect(path).kind, "msi")

    def test_malformed_msi_is_unknown_never_a_hang(self):
        for data in (msi_bytes(loop=True), msi_bytes(stream_name="Other"), msi_bytes(None),
                     installer.OLE_MAGIC + b"\0" * 100, msi_bytes()[:700]):
            self.assertIsNone(installer.inspect(self.installer_file("bad.msi", data)).machine)

    def test_32_bit_msi_is_refused_and_64_bit_msi_installs(self):
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(self.installer_file("x86.msi", msi_bytes("Intel;1033"))))
        self.assertIn(UNSUPPORTED_32BIT_MESSAGE, str(cm.exception))
        result = ops.install(self.ctx(), str(self.installer_file("x64.msi", msi_bytes("x64;1033"))),
                             app_id="local.msi-app")
        self.assertEqual(result["application"]["state"], "installed")
        installer_call = next(c for c in self.executor.calls if c["title"] == "installer")
        self.assertEqual(installer_call["command"][1], "msiexec")
        record = store.AppStore(self.home).read_record("local.msi-app")
        self.assertEqual(record["architecture"], "x86_64")

    def test_32_bit_catalog_entry_is_unsupported_and_never_installed(self):
        exe = self.installer_file()
        sha = hashlib.sha256(exe.read_bytes()).hexdigest()
        self.add_manifest(architecture="x86", installer={"sha256": sha})
        entry = ops.catalog_list(self.ctx())["manifests"][0]
        self.assertEqual((entry["supported"], entry["compatibility"]), (False, "UNSUPPORTED_ARCHITECTURE"))
        self.assertIn(UNSUPPORTED_32BIT_MESSAGE, entry["support_message"])
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(exe), app_id="com.example.app")
        self.assertEqual(cm.exception.reason, "architecture")
        self.assertEqual(self.executor.calls, [])

    def test_runtime_state_of_an_unsupported_application(self):
        exe = self.installer_file()
        sha = hashlib.sha256(exe.read_bytes()).hexdigest()
        self.add_manifest(installer={"sha256": sha}, launch="Program Files/Fake App/fake.exe")
        ops.install(self.ctx(), str(exe))
        self.add_manifest(installer={"sha256": sha}, launch="Program Files/Fake App/fake.exe", architecture="x86")
        app = ops.list_apps(self.ctx())["applications"][0]
        self.assertEqual((app["app_state"], app["supported"], app["compatibility"]),
                         ("UNSUPPORTED", False, "UNSUPPORTED_ARCHITECTURE"))
        with self.assertRaises(Refused) as cm:
            ops.launch(self.ctx(), "com.example.app", [])
        self.assertEqual(cm.exception.reason, "architecture")


class InspectTests(CompatTestCase):
    def test_inspect_decides_without_creating_or_running_anything(self):
        result = ops.inspect_installer(self.ctx(), str(self.installer_file("Tool Setup.exe")))
        self.assertTrue(result["decision"]["allowed"])
        self.assertEqual(result["application"]["id"], "local.tool-setup")
        self.assertEqual((result["installer"]["kind"], result["installer"]["machine"]), ("exe", "x86_64"))
        self.assertEqual(result["architecture"]["supported"], True)
        self.assertEqual(result["installer"]["framework"], "NSIS")
        self.assertEqual(self.executor.calls, [])
        self.assertFalse((self.home / ".local/share/boswas/wine").exists())

    def test_inspect_reports_refusals(self):
        r = ops.inspect_installer(self.ctx(), str(self.installer_file("x86.exe", pe_bytes(machine=0x14C))))
        self.assertEqual((r["decision"]["allowed"], r["decision"]["reason"]), (False, "architecture"))
        self.assertIn(UNSUPPORTED_32BIT_MESSAGE, r["decision"]["message"])
        self.assertIs(r["architecture"]["supported"], False)
        exe = self.installer_file()
        self.add_manifest(status="blocked", installer={"sha256": hashlib.sha256(exe.read_bytes()).hexdigest()})
        r = ops.inspect_installer(self.ctx(), str(exe))
        self.assertEqual(r["decision"]["reason"], "blocked")

    def test_inspect_already_installed(self):
        exe = self.installer_file()
        ops.install(self.ctx(), str(exe), app_id="local.twice")
        r = ops.inspect_installer(self.ctx(), str(exe), app_id="local.twice")
        self.assertEqual((r["decision"]["allowed"], r["decision"]["reason"]), (False, "already-installed"))

    def test_cli_inspect_exit_codes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(ops.Context, "create", lambda out=None: self.ctx(out)):
            ok = cli.main(["--json", "inspect", str(self.installer_file())])
            refused = cli.main(["inspect", str(self.installer_file("x86.exe", pe_bytes(machine=0x14C)))])
        self.assertEqual((ok, refused), (0, 4))
        self.assertTrue(json.loads(out.getvalue().split("\n}\n")[0] + "}")["decision"]["allowed"])
        self.assertIn("32-bit Windows compatibility", out.getvalue())


class StopTests(CompatTestCase):
    def setUp(self):
        super().setUp()
        ops.install(self.ctx(), str(self.installer_file()), app_id="local.fake")
        self.s = store.AppStore(self.home)
        self.lock = self.home / ".local/share/boswas/wine/local.fake/.lock"

    def hold_lock(self):
        proc = start_process(self, [sys.executable, "-c", LOCK_HOLDER, str(self.lock)], stdout=subprocess.PIPE,
                             text=True)
        self.assertEqual(proc.stdout.readline().strip(), "locked")
        return proc

    def test_stop_ends_the_verified_launcher(self):
        proc = self.hold_lock()
        self.s.write_run("local.fake", {"operation": "launch", "pid": proc.pid})
        self.assertEqual(self.s.lock_holder("local.fake"), proc.pid)
        result = ops.stop(self.ctx(), "local.fake", wait=10)
        self.assertEqual((result["was_running"], result["stopped"]), (True, True))
        self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM)

    def test_stop_never_signals_an_unverified_process(self):
        holder = self.hold_lock()
        bystander = start_process(self, [sys.executable, "-c", "import time; time.sleep(60)"])
        self.s.write_run("local.fake", {"operation": "launch", "pid": bystander.pid})
        with self.assertRaises(Refused) as cm:
            ops.stop(self.ctx(), "local.fake", wait=2)
        self.assertEqual(cm.exception.reason, "stop-unverified")
        time.sleep(0.3)
        self.assertIsNone(bystander.poll())
        self.assertIsNone(holder.poll())

    def test_stop_refuses_other_operations_and_reports_idle_apps(self):
        self.assertEqual(ops.stop(self.ctx(), "local.fake")["was_running"], False)
        self.hold_lock()                                   # an operation without run.json
        with self.assertRaises(Busy):
            ops.stop(self.ctx(), "local.fake")
        self.s.write_run("local.fake", {"operation": "launch", "pid": True})
        with self.assertRaises(Busy):
            ops.stop(self.ctx(), "local.fake")

    def test_launch_is_stoppable_and_records_a_stop(self):
        real_run = self.executor.run
        seen = {}

        def run(argv, log_path, **kw):
            seen["run"] = self.s.read_run("local.fake")
            result = real_run(argv, log_path, **kw)
            result.stopped = True
            return result
        self.executor.run = run
        ops.launch(self.ctx(), "local.fake", [])
        self.assertEqual(seen["run"]["operation"], "launch")
        self.assertEqual(seen["run"]["pid"], os.getpid())
        self.assertTrue(self.executor.calls[-1]["stoppable"])
        self.assertIsNone(self.s.read_run("local.fake"))      # cleared afterwards
        self.assertTrue(self.s.read_record("local.fake")["last_launch"]["stopped"])
        self.assertEqual(ops.list_apps(self.ctx())["applications"][0]["app_state"], "STOPPED")

    def test_real_executor_stops_on_sigterm(self):
        script = self.tmp / "fake-bwrap"
        script.write_text("#!/bin/sh\necho 'boswas-winapp-exec: confinement=x' >&2\nexec sleep 30\n")
        script.chmod(0o755)
        timer = threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
        started = time.monotonic()
        result = executor.Executor().run([str(script)], self.tmp / "log", stoppable=True)
        self.assertTrue(result.stopped)
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("stopped on request", (self.tmp / "log").read_text())
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


class UpgradeTests(CompatTestCase):
    def setUp(self):
        super().setUp()
        self.v1 = self.installer_file("app-1.exe", pe_bytes(extra=b"Nullsoft Install System v1"))
        self.v2 = self.installer_file("app-2.exe", pe_bytes(extra=b"Nullsoft Install System v2"))
        self.pin(self.v1, "1.0")
        ops.install(self.ctx(), str(self.v1))

    def pin(self, exe, version, **extra):
        self.add_manifest(version=version, status="tested", launch="Program Files/Fake App/fake.exe",
                          installer={"sha256": hashlib.sha256(exe.read_bytes()).hexdigest()}, **extra)

    def test_upgrade_to_the_newly_pinned_installer(self):
        self.pin(self.v2, "2.0")
        self.assertEqual(ops.list_apps(self.ctx())["applications"][0]["app_state"], "BLOCKED")   # needs upgrade
        result = ops.upgrade(self.ctx(), "com.example.app", str(self.v2))
        self.assertEqual((result["previous"]["version"], result["application"]["version"]), ("1.0", "2.0"))
        app = ops.list_apps(self.ctx())["applications"][0]
        self.assertEqual((app["app_state"], app["allowed"]), ("INSTALLED", True))
        prefix_runs = [c for c in self.executor.calls if "wineboot" in c["command"]]
        self.assertEqual(len(prefix_runs), 1)              # the existing prefix was kept

    def test_upgrade_refusals(self):
        with self.assertRaises(Refused) as cm:
            ops.upgrade(self.ctx(), "com.example.app", str(self.v1))
        self.assertEqual(cm.exception.reason, "up-to-date")
        with self.assertRaises(Refused) as cm:
            ops.upgrade(self.ctx(), "com.example.app", str(self.v2))     # not the pinned installer
        self.assertEqual(cm.exception.reason, "installer-mismatch")
        with self.assertRaises(Refused) as cm:
            ops.upgrade(self.ctx(), "com.example.app", str(self.installer_file("x86.exe", pe_bytes(machine=0x14C))))
        self.assertEqual(cm.exception.reason, "architecture")

    def test_failed_upgrade_is_an_error_that_repair_can_clear(self):
        self.pin(self.v2, "2.0")
        self.executor.exit_code = 3
        with self.assertRaises(WinAppError):
            ops.upgrade(self.ctx(), "com.example.app", str(self.v2))
        app = ops.list_apps(self.ctx())["applications"][0]
        self.assertEqual(app["app_state"], "ERROR")
        self.assertIn("upgrade failed", app["error"])
        self.pin(self.v1, "1.0")
        self.executor.exit_code = 0
        self.assertTrue(ops.repair(self.ctx(), "com.example.app")["healthy"])
        self.assertEqual(ops.list_apps(self.ctx())["applications"][0]["app_state"], "INSTALLED")


class StateAndLogTests(CompatTestCase):
    def setUp(self):
        super().setUp()
        ops.install(self.ctx(), str(self.installer_file()), app_id="local.fake")
        self.appdir = self.home / ".local/share/boswas/wine/local.fake"

    def state(self):
        return ops.list_apps(self.ctx())["applications"][0]

    def test_states(self):
        app = self.state()
        self.assertEqual((app["app_state"], app["architecture"], app["supported"]), ("INSTALLED", "x86_64", True))
        s = store.AppStore(self.home)
        with s.lock("local.fake"):
            s.write_run("local.fake", {"operation": "launch", "pid": os.getpid()})
            self.assertEqual(self.state()["app_state"], "RUNNING")
        s.clear_run("local.fake")
        (self.appdir / "sandbox/prefix/drive_c/Program Files/Fake App/fake.exe").unlink()
        app = self.state()
        self.assertEqual(app["app_state"], "REPAIR_REQUIRED")
        self.assertIn("missing", app["state_reason"])
        record = s.read_record("local.fake")
        s.write_record("local.fake", {**record, "state": "failed", "error": "installer exited with code 2"})
        self.assertEqual(self.state()["app_state"], "ERROR")
        st = ops.status(self.ctx(), "local.fake")
        self.assertEqual(st["application"]["app_state"], "ERROR")
        self.assertEqual(set(ops.APP_STATES) >= {"INSTALLED", "RUNNING", "STOPPED", "ERROR", "REPAIR_REQUIRED",
                                                  "BLOCKED", "UNSUPPORTED"}, True)

    def test_clear_logs_never_follows_links_and_waits_for_the_app(self):
        outside = self.tmp / "precious.txt"
        outside.write_text("keep me")
        os.symlink(outside, self.appdir / "logs/evil.log")
        removed = ops.clear_logs(self.ctx(), "local.fake")["removed"]
        self.assertGreaterEqual(removed, 1)
        self.assertEqual(outside.read_text(), "keep me")
        self.assertEqual([p.name for p in (self.appdir / "logs").iterdir()], ["evil.log"])
        with store.AppStore(self.home).lock("local.fake"):
            with self.assertRaises(Busy):
                ops.clear_logs(self.ctx(), "local.fake")

    def test_all_users_inventory_reports_running_applications(self):
        lock = self.appdir / ".lock"
        proc = start_process(self, [sys.executable, "-c", LOCK_HOLDER, str(lock)], stdout=subprocess.PIPE, text=True)
        proc.stdout.readline()
        entry = mock.Mock(pw_uid=os.getuid(), pw_dir=str(self.home), pw_name="tester")
        with mock.patch.object(os, "geteuid", return_value=0), mock.patch("pwd.getpwall", return_value=[entry]):
            users = store.list_all_users(min_uid=0)
        self.assertEqual(users[0]["running"], ["local.fake"])
        self.assertEqual(users[0]["applications"][0]["architecture"], "x86_64")
        proc.kill()
        proc.wait()
        # A FIFO planted as the lock file neither blocks nor counts as running.
        lock.unlink()
        os.mkfifo(lock)
        self.assertFalse(store.probe_running(self.appdir, os.getuid()))


class PolicyListTests(CompatTestCase):
    def test_blocked_and_allowed_application_lists(self):
        self.set_policy(BLOCKED_APPLICATIONS="local.bad not_an_id")
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(self.installer_file()), app_id="local.bad")
        self.assertEqual(cm.exception.reason, "policy-blocked")
        self.assertIn("ignoring invalid application ID", " ".join(policy.Policy.load().problems))
        ops.install(self.ctx(), str(self.installer_file()), app_id="local.good")
        self.set_policy(ALLOWED_APPLICATIONS="local.other")
        app = ops.list_apps(self.ctx())["applications"][0]
        self.assertEqual((app["app_state"], app["allowed"]), ("BLOCKED", False))
        with self.assertRaises(Refused):
            ops.launch(self.ctx(), "local.good", [])
        with self.assertRaises(Refused):
            ops.repair(self.ctx(), "local.good")

    def managed(self, text, mode=0o644):
        path = self.root / "var/lib/boswas/compat/policy.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_managed_policy_replaces_the_local_file(self):
        self.managed('ALLOWED_STATUSES="approved"\nUNLISTED_APPS="deny"\nREQUIRE_APPARMOR="yes"\n')
        p = policy.Policy.load()
        self.assertTrue(p.managed)
        self.assertFalse(p.unlisted_apps)
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(self.installer_file()))
        self.assertEqual(cm.exception.reason, "unlisted-denied")

    def test_untrusted_managed_policy_falls_back_to_restrictive_defaults(self):
        path = self.managed('UNLISTED_APPS="allow"\nALLOWED_STATUSES="unknown"\n', mode=0o666)
        p = policy.Policy.load()
        self.assertEqual((p.managed, p.unlisted_apps, p.require_apparmor), (True, False, True))
        self.assertIn("writable by group or others", p.problems[0])
        path.unlink()
        os.symlink(COMPAT / "policies/policy.conf", path)
        p = policy.Policy.load()
        self.assertFalse(p.unlisted_apps)
        self.assertIn("not a regular file", p.problems[0])


class RuntimeAndCommandTests(CompatTestCase):
    def test_runtime_health(self):
        r = ops.runtime_info(self.ctx())
        self.assertTrue(r["wine"]["available"])
        self.assertEqual(r["architectures"], ["x86_64"])
        self.assertEqual(r["apparmor"]["mode"], "unavailable")     # no AppArmor in the fake root
        self.assertFalse(r["healthy"])                             # required by policy, not available
        self.set_policy(REQUIRE_APPARMOR="no")
        self.assertTrue(ops.runtime_info(self.ctx())["healthy"])

    def test_installed_command_ignores_test_hooks(self):
        text = (HERE.parents[1] / "bin/boswas-winapp").read_text()
        hooks = text.index('for _hook in ("BOSWAS_SYSROOT", "BOSWAS_WINAPP_HOME")')
        self.assertLess(hooks, text.index("from boswas_compat.cli import main"))

    def test_cli_stop_upgrade_and_clear(self):
        ops.install(self.ctx(), str(self.installer_file()), app_id="local.cli")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(ops.Context, "create", lambda out=None: self.ctx(out)):
            self.assertEqual(cli.main(["--json", "stop", "local.cli"]), 0)
            self.assertEqual(cli.main(["logs", "local.cli", "--clear"]), 0)
            self.assertEqual(cli.main(["upgrade", "local.cli", str(self.installer_file())]), 4)   # same installer
            self.assertEqual(cli.main(["runtime"]), 1)
        self.assertIn('"was_running": false', out.getvalue())


if __name__ == "__main__":
    unittest.main()
