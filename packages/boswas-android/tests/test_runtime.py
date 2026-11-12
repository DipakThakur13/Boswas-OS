"""Status, compatibility checks, Waydroid parsing, session, apps, sysops pass-through, F-Droid, cleanup.

The real code runs against a temporary root and a recording fake runner;
no process is started and nothing outside the temporary directory is touched.

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import io
import json
import os
import sys
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import android_fixtures as fx  # noqa: E402

from boswas_android import apk, checks, config, googleplay, runtime, sysops  # noqa: E402
from boswas_android import runner as runner_mod  # noqa: E402
from boswas_android import waydroid as wd  # noqa: E402
from boswas_android.errors import (Failed, InvalidArgument, NotInitialized, NotRunning, Refused,  # noqa: E402
                                   RuntimeMissing, Unsupported)
from boswas_android.system import Root  # noqa: E402

WAYDROID = runner_mod.EXECUTABLES["waydroid"]
APP_LIST = ("Name: Boswas Test\npackageName: com.boswas.testapp\ncategories:\n\tandroid.intent.category.LAUNCHER\n"
            "Name: Settings\npackageName: com.android.settings\ncategories:\n\tandroid.intent.category.LAUNCHER\n")


class ParserTest(unittest.TestCase):
    def test_status_running(self):
        parsed = wd.parse_status(fx.STATUS_RUNNING)
        self.assertEqual((parsed["running"], parsed["state"], parsed["ip"]), (True, "RUNNING", "192.168.240.112"))
        self.assertTrue(parsed["initialized"])

    def test_status_stopped_and_frozen(self):
        self.assertFalse(wd.parse_status(fx.STATUS_STOPPED)["running"])
        frozen = wd.parse_status(fx.STATUS_RUNNING.replace("Container:\tRUNNING", "Container:\tFROZEN"))
        self.assertEqual((frozen["running"], frozen["state"]), (True, "FROZEN"))

    def test_status_not_initialized(self):
        parsed = wd.parse_status('Waydroid is not initialized, run "waydroid init"\n')
        self.assertFalse(parsed["initialized"])
        self.assertFalse(parsed["running"])

    def test_app_list(self):
        apps = wd.parse_app_list(APP_LIST + "[12:00:01] some log line\n")
        self.assertEqual([a["package"] for a in apps], ["com.boswas.testapp", "com.android.settings"])
        self.assertEqual(apps[0]["categories"], ["android.intent.category.LAUNCHER"])
        self.assertIsNone(apps[0]["version"])

    def test_output_errors(self):
        self.assertEqual(wd.output_error("[10:00:00] WayDroid session is stopped\n"), "WayDroid session is stopped")
        self.assertIsNotNone(wd.output_error("Failed to access IPlatform service"))
        self.assertIsNone(wd.output_error("Installing...\n"))

    def test_dpkg_version(self):
        self.assertEqual(wd.dpkg_version(fx.DPKG_WAYDROID, "waydroid"), "1.6.3")
        removed = fx.DPKG_WAYDROID.replace("install ok installed", "deinstall ok config-files", 1)
        self.assertIsNone(wd.dpkg_version(removed, "waydroid"))
        self.assertIsNone(wd.dpkg_version(None, "waydroid"))

    def test_image_version_and_type(self):
        self.assertEqual(wd.image_version("1790000000"), "2026-09-21T14:13:20Z")
        self.assertIsNone(wd.image_version("0"))
        self.assertIsNone(wd.image_version("x"))
        self.assertEqual(config.image_type_from_ota("https://ota.waydro.id/system/lineage/waydroid_x86_64/VANILLA.json"),
                         "VANILLA")
        self.assertIsNone(config.image_type_from_ota("None"))

    def test_bridge_address(self):
        doc = json.dumps([{"ifname": "waydroid0", "addr_info": [{"family": "inet", "local": "192.168.240.1"}]}])
        self.assertEqual(wd.bridge_address(doc), "192.168.240.1")
        self.assertIsNone(wd.bridge_address("not json"))


class ChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = fx.tempdir(self)

    def checks(self, environ=None, machine="x86_64", **root_kwargs):
        root = fx.make_root(self.tmp, **root_kwargs)
        ctx = fx.make_ctx(self.tmp, root, environ=environ, machine=machine)
        compat = checks.compatibility(ctx.root, ctx.host)
        return compat, {c["id"]: c for c in compat["checks"]}

    def test_ids_order_and_shape(self):
        compat, by_id = self.checks()
        self.assertEqual([c["id"] for c in compat["checks"]], list(checks.IDS))
        for check in compat["checks"]:
            self.assertEqual(set(check), {"id", "status", "title", "detail"})
            self.assertIn(check["status"], ("PASS", "WARN", "FAIL", "UNKNOWN"))
        self.assertTrue(compat["supported"])
        self.assertEqual(by_id["gpu"]["status"], "PASS")
        self.assertIn("i915", by_id["gpu"]["detail"])

    def test_binder(self):
        self.assertEqual(self.checks(binder="module")[1]["binder"]["status"], "PASS")
        self.tmp = fx.tempdir(self)
        self.assertEqual(self.checks(binder="node")[1]["binder"]["status"], "PASS")
        self.tmp = fx.tempdir(self)
        compat, by_id = self.checks(binder=None)
        self.assertEqual(by_id["binder"]["status"], "FAIL")
        self.assertFalse(compat["supported"])

    def test_wayland(self):
        self.assertEqual(self.checks()[1]["wayland"]["status"], "PASS")
        self.tmp = fx.tempdir(self)
        x11 = self.checks(environ={"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": ""})[1]["wayland"]
        self.assertEqual(x11["status"], "FAIL")
        self.assertIn("Plasma (Wayland)", x11["detail"])
        self.tmp = fx.tempdir(self)
        self.assertEqual(self.checks(environ={"XDG_SESSION_TYPE": "", "WAYLAND_DISPLAY": ""})[1]["wayland"]["status"],
                         "UNKNOWN")

    def test_cpu(self):
        self.assertEqual(self.checks(machine="aarch64")[1]["cpu"]["status"], "FAIL")
        self.tmp = fx.tempdir(self)
        no_ssse3 = "flags\t: fpu sse sse2\n"
        self.assertEqual(self.checks(cpuinfo=no_ssse3)[1]["cpu"]["status"], "FAIL")
        self.tmp = fx.tempdir(self)
        no_sse42 = "flags\t: fpu sse sse2 ssse3 sse4_1\n"
        self.assertEqual(self.checks(cpuinfo=no_sse42)[1]["cpu"]["status"], "WARN")
        root = Root(fx.make_root(fx.tempdir(self), cpuinfo=no_sse42))
        self.assertEqual(checks.runtime_abis(root), ("x86",))
        self.assertEqual(checks.runtime_abis(root, {"arch": "x86_64"}), ("x86_64", "x86"))
        self.assertEqual(checks.runtime_abis(Root(fx.make_root(fx.tempdir(self)))), ("x86_64", "x86"))

    def test_gpu(self):
        nvidia = self.checks(gpu_driver="nvidia")[1]["gpu"]
        self.assertEqual(nvidia["status"], "WARN")
        self.assertIn("NVIDIA", nvidia["detail"])
        self.tmp = fx.tempdir(self)
        none = self.checks(gpu_driver=None)[1]["gpu"]
        self.assertEqual(none["status"], "WARN")
        self.assertIn("software rendering", none["detail"])

    def test_secure_boot_never_fails(self):
        for state in (1, 0, None):
            self.tmp = fx.tempdir(self)
            check = self.checks(secure_boot=state)[1]["secure-boot"]
            self.assertIn(check["status"], ("PASS", "UNKNOWN"))
        self.tmp = fx.tempdir(self)
        self.assertIn("signed", self.checks(secure_boot=1)[1]["secure-boot"]["detail"])

    def test_disk(self):
        root = fx.make_root(self.tmp)
        ctx = fx.make_ctx(self.tmp, root)
        with mock.patch.object(Root, "free_bytes", return_value=1024 ** 3):
            check = checks.disk_check(ctx.root, ctx.host, None, False)
        self.assertEqual(check["status"], "FAIL")
        with mock.patch.object(Root, "free_bytes", return_value=50 * 1024 ** 3):
            self.assertEqual(checks.disk_check(ctx.root, ctx.host, None, False)["status"], "PASS")


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = fx.tempdir(self)

    def test_not_installed(self):
        ctx = fx.make_ctx(self.tmp, fx.make_root(self.tmp))
        doc = runtime.status(ctx)
        self.assertEqual(doc["schema"], "boswas-android/1")
        self.assertEqual(doc["runtime"], {"installed": False, "version": None, "repository": False})
        self.assertFalse(doc["initialized"])
        self.assertEqual(doc["images"], {"type": None, "system": None, "vendor": None, "bytes": None})
        self.assertEqual(doc["session"], {"running": False, "state": None})
        self.assertEqual(doc["service"], {"active": False})
        self.assertEqual(doc["storage"], {"images_bytes": None, "user_bytes": 0})     # no Android data yet
        self.assertEqual(doc["network"], {"interface": "waydroid0", "present": False, "address": None})
        self.assertEqual(doc["google_play"]["available"], False)
        self.assertEqual(doc["google_play"]["alternatives"], ["fdroid", "local-apk"])
        self.assertEqual(ctx.runner.calls, [])            # nothing asked of a runtime that is not there
        self.assertEqual(set(doc), {"schema", "runtime", "compatibility", "initialized", "images", "service",
                                    "session", "storage", "network", "gpu", "google_play"})

    def test_installed_initialized_running(self):
        root = fx.make_root(self.tmp, installed=True, initialized=True)
        fx.write(root, "/etc/apt/sources.list.d/boswas-waydroid.sources", "Types: deb\n")
        (Path(root) / "sys/class/net/waydroid0").mkdir(parents=True)
        runner = fx.FakeRunner()
        runner.answers[("status",)] = fx.done(fx.STATUS_RUNNING)
        runner.answers[("is-active", "waydroid-container.service")] = fx.done("active\n")
        runner.answers[("-j", "-4", "addr", "show", "dev", "waydroid0")] = fx.done(json.dumps(
            [{"addr_info": [{"family": "inet", "local": "192.168.240.1"}]}]))
        ctx = fx.make_ctx(self.tmp, root, runner)
        fx.write(os.path.join(self.tmp, "home"), "/.local/share/waydroid/data/file", b"x" * 10)
        doc = runtime.status(ctx)
        self.assertEqual(doc["runtime"], {"installed": True, "version": "1.6.3", "repository": True})
        self.assertTrue(doc["initialized"])
        self.assertEqual(doc["images"]["type"], "VANILLA")
        self.assertEqual(doc["images"]["system"], "2026-09-21T14:13:20Z")
        self.assertEqual(doc["images"]["bytes"], 4000)
        self.assertEqual(doc["storage"]["images_bytes"], 4000)
        self.assertGreater(doc["storage"]["user_bytes"], 0)
        self.assertEqual(doc["service"], {"active": True})
        self.assertEqual(doc["session"], {"running": True, "state": "RUNNING"})
        self.assertEqual(doc["network"], {"interface": "waydroid0", "present": True, "address": "192.168.240.1"})
        self.assertEqual(doc["gpu"]["acceleration"], "hardware")

    def test_installed_not_initialized_does_not_query_the_session(self):
        runner = fx.FakeRunner()
        ctx = fx.make_ctx(self.tmp, fx.make_root(self.tmp, installed=True), runner)
        doc = runtime.status(ctx)
        self.assertFalse(doc["initialized"])
        self.assertNotIn([WAYDROID, "status"], runner.calls)

    def test_google_play_is_never_available(self):
        doc = googleplay.detailed()
        self.assertFalse(doc["available"])
        self.assertIn("licen", doc["reason"])
        self.assertEqual(doc["alternatives"], ["fdroid", "local-apk"])
        self.assertIn("not Google Play", doc["alternative_labels"]["fdroid"])


class SysopsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = fx.tempdir(self)

    def test_install_refused_locally_on_unsupported_hardware(self):
        runner = fx.FakeRunner()
        ctx = fx.make_ctx(self.tmp, fx.make_root(self.tmp, binder=None), runner)
        with self.assertRaises(Unsupported) as caught:
            runtime.runtime_operation(ctx, "install")
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertEqual(runner.streams, [])

    def test_pass_through_events_and_exit_code(self):
        runner = fx.FakeRunner()
        lines = [{"type": "start", "schema": "boswas-sysops/1", "operation": "android-runtime-install",
                  "cancellable": True},
                 {"type": "progress", "phase": "download", "percent": 50.0, "message": "Downloading waydroid"},
                 {"type": "result", "ok": True, "code": "OK", "message": "Installed.", "data": {}}]
        runner.stream_lines = [json.dumps(line) for line in lines] + ["not json from pkexec"]
        ctx = fx.make_ctx(self.tmp, fx.make_root(self.tmp), runner)
        events = []
        outcome = runtime.runtime_operation(ctx, "install", on_event=events.append)
        self.assertEqual(runner.streams, [[runner_mod.EXECUTABLES["pkexec"], runner_mod.SYSOPS,
                                           "android-runtime-install"]])
        self.assertEqual(events[:3], lines)
        self.assertEqual(events[3], {"type": "log", "level": "info", "message": "not json from pkexec"})
        self.assertEqual(outcome.exit_code, 0)
        self.assertTrue(outcome.result["ok"])

    def test_not_authorized_is_synthesised(self):
        runner = fx.FakeRunner()
        runner.stream_code = 126
        root = fx.make_root(self.tmp, installed=True)
        outcome = runtime.service_operation(fx.make_ctx(self.tmp, root, runner), "start")
        self.assertEqual(outcome.exit_code, 126)
        self.assertEqual(outcome.result["code"], "NOT_AUTHORIZED")
        self.assertEqual([e["type"] for e in outcome.lines].count("result"), 1)

    def test_missing_result_is_synthesised(self):
        runner = fx.FakeRunner()
        runner.stream_code = 1
        outcome = sysops.run(runner, "android-init")
        self.assertEqual(outcome.result["code"], "FAILED")

    def test_operation_preconditions_and_arguments(self):
        runner = fx.FakeRunner()
        ctx = fx.make_ctx(self.tmp, fx.make_root(self.tmp), runner)
        with self.assertRaises(RuntimeMissing):
            runtime.runtime_operation(ctx, "init")
        root = fx.make_root(fx.tempdir(self), installed=True)
        ctx = fx.make_ctx(self.tmp, root, runner)
        with self.assertRaises(NotInitialized):
            runtime.runtime_operation(ctx, "repair")
        runtime.runtime_operation(ctx, "remove", purge_data=True)
        self.assertEqual(runner.streams[-1][2:], ["android-runtime-remove", "--purge-data"])
        with self.assertRaises(InvalidArgument):
            runtime.runtime_operation(ctx, "format-disk")

    def test_real_stream_forwards_cancel(self):
        """The real Runner.stream against a stand-in helper: lines arrive, "cancel" reaches its stdin."""
        script = Path(self.tmp) / "helper.py"
        script.write_text(textwrap.dedent("""\
            import json, sys
            print(json.dumps({"type": "start", "schema": "boswas-sysops/1", "operation": sys.argv[1],
                              "cancellable": True}), flush=True)
            line = sys.stdin.readline()
            ok = line.strip() == "cancel"
            print(json.dumps({"type": "result", "ok": False, "code": "CANCELLED" if ok else "FAILED",
                              "message": "cancelled" if ok else repr(line), "data": {}}), flush=True)
            sys.exit(4 if ok else 1)
            """), encoding="utf-8")
        executables = dict(runner_mod.EXECUTABLES, pkexec=sys.executable)
        with mock.patch.object(runner_mod, "EXECUTABLES", executables), \
                mock.patch.object(runner_mod, "ALLOWED_PATHS", frozenset(executables.values())), \
                mock.patch.object(runner_mod, "SYSOPS", str(script)):
            cancel = threading.Event()
            events = []

            def on_event(event):
                events.append(event)
                if event["type"] == "start":
                    cancel.set()
            outcome = sysops.run(runner_mod.Runner({"PATH": "/usr/bin"}), "android-init", (), on_event, cancel)
        self.assertEqual(outcome.exit_code, 4)
        self.assertEqual(outcome.result["code"], "CANCELLED")


class SessionAndAppsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = fx.tempdir(self)
        self.root = fx.make_root(self.tmp, installed=True, initialized=True)
        self.runner = fx.FakeRunner()
        self.ctx = fx.make_ctx(self.tmp, self.root, self.runner)

    def apk(self, abis=("x86_64",), package="com.boswas.testapp"):
        return fx.build_apk(os.path.join(self.tmp, "app.apk"), fx.manifest(package=package), abis,
                            pairs={apk.SIG_V2: fx.v2_value([b"\x30\x00"])})

    def test_session_start_starts_the_service_then_the_session(self):
        states = iter([fx.STATUS_STOPPED, fx.STATUS_STOPPED, fx.STATUS_RUNNING])
        self.runner.answers[("status",)] = lambda: fx.done(next(states))
        self.runner.answers[("is-active", "waydroid-container.service")] = fx.done("inactive\n")
        self.runner.stream_lines = [json.dumps({"type": "result", "ok": True, "code": "OK", "message": "started",
                                                "data": {}})]
        doc = runtime.session_start(self.ctx)
        self.assertEqual(doc["session"], {"running": True, "already": False})
        self.assertEqual(self.runner.streams[0][2:], ["android-service", "start"])
        self.assertEqual(self.runner.started, [[WAYDROID, "session", "start"]])

    def test_session_start_needs_wayland(self):
        ctx = fx.make_ctx(self.tmp, self.root, self.runner, environ={"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": ""})
        with self.assertRaises(Unsupported):
            runtime.session_start(ctx)
        self.assertEqual(self.runner.started, [])

    def test_session_start_times_out_honestly(self):
        self.runner.answers[("status",)] = fx.done(fx.STATUS_STOPPED)
        self.runner.answers[("is-active", "waydroid-container.service")] = fx.done("active\n")
        with self.assertRaises(Failed):
            runtime.session_start(self.ctx)

    def test_incompatible_apk_is_refused_before_android_is_asked(self):
        with self.assertRaises(Refused) as caught:
            runtime.apk_install(self.ctx, self.apk(abis=("arm64-v8a",)))
        self.assertEqual((caught.exception.code, caught.exception.exit_code), ("INCOMPATIBLE", 2))
        self.assertTrue(caught.exception.problems)
        self.assertEqual(self.runner.calls, [])

    def test_install_needs_a_running_session(self):
        self.runner.answers[("status",)] = fx.done(fx.STATUS_STOPPED)
        with self.assertRaises(NotRunning) as caught:
            runtime.apk_install(self.ctx, self.apk())
        self.assertEqual(caught.exception.exit_code, 3)

    def test_install_success_is_confirmed_by_the_app_list(self):
        self.runner.answers[("status",)] = fx.done(fx.STATUS_RUNNING)
        lists = iter(["Name: Settings\npackageName: com.android.settings\ncategories:\n", APP_LIST])
        self.runner.answers[("app", "list")] = lambda: fx.done(next(lists, APP_LIST))
        path = self.apk()
        doc = runtime.apk_install(self.ctx, path)
        self.assertEqual(doc["installed"]["package"], "com.boswas.testapp")
        self.assertFalse(doc["installed"]["updated"])
        self.assertIn([WAYDROID, "app", "install", os.path.abspath(path)], self.runner.calls)

    def test_install_failure_reported_in_output(self):
        self.runner.answers[("status",)] = fx.done(fx.STATUS_RUNNING)
        path = os.path.abspath(self.apk())
        self.runner.answers[("app", "install", path)] = fx.done("[10:00:00] Failed to access IPlatform service\n")
        with self.assertRaises(Failed):
            runtime.apk_install(self.ctx, path)

    def test_install_not_confirmed_is_a_failure(self):
        self.runner.answers[("status",)] = fx.done(fx.STATUS_RUNNING)
        self.runner.answers[("app", "list")] = fx.done("")
        with self.assertRaises(Failed) as caught:
            runtime.apk_install(self.ctx, self.apk())
        self.assertIn("did not report", caught.exception.message)

    def test_remove_and_launch(self):
        self.runner.answers[("status",)] = fx.done(fx.STATUS_RUNNING)
        self.runner.answers[("app", "list")] = fx.done("")
        self.assertEqual(runtime.app_remove(self.ctx, "com.boswas.testapp")["removed"]["package"], "com.boswas.testapp")
        runtime.app_launch(self.ctx, "com.boswas.testapp")
        self.assertEqual(self.runner.started[-1], [WAYDROID, "app", "launch", "com.boswas.testapp"])
        for bad in ("../etc", "com", "a b.c", "-x.y", "com.example;rm", "x" * 300):
            with self.assertRaises(InvalidArgument):
                runtime.app_launch(self.ctx, bad)

    def test_app_list_requires_running(self):
        self.runner.answers[("status",)] = fx.done(fx.STATUS_STOPPED)
        with self.assertRaises(NotRunning):
            runtime.app_list(self.ctx)


class FdroidTest(unittest.TestCase):
    def setUp(self):
        self.tmp = fx.tempdir(self)

    def ctx_with(self, apk_bytes, cert_sha256, url="https://f-droid.org/F-Droid.apk"):
        root = fx.make_root(self.tmp, installed=True, initialized=True,
                            conf_overrides={"FDROID_CERT_SHA256": cert_sha256, "FDROID_URL": url})
        runner = fx.FakeRunner()
        runner.answers[("status",)] = fx.done(fx.STATUS_RUNNING)
        lists = iter(["", "Name: F-Droid\npackageName: org.fdroid.fdroid\ncategories:\n"])
        runner.answers[("app", "list")] = lambda: fx.done(next(lists, "Name: F-Droid\npackageName: org.fdroid.fdroid\n"))

        class Response(io.BytesIO):
            def geturl(self):
                return url
        opener = (lambda u, timeout: Response(apk_bytes))
        return fx.make_ctx(self.tmp, root, runner, opener=opener), runner

    def fdroid_apk(self, cert=b"\x30\x01\x00"):
        path = fx.build_apk(os.path.join(self.tmp, "src.apk"), fx.manifest(package="org.fdroid.fdroid"), ("x86_64",),
                            pairs={apk.SIG_V2: fx.v2_value([cert])})
        return Path(path).read_bytes()

    def test_installs_when_the_certificate_matches(self):
        import hashlib
        cert = b"\x30\x01\x00"
        ctx, runner = self.ctx_with(self.fdroid_apk(cert), hashlib.sha256(cert).hexdigest())
        doc = runtime.fdroid_install(ctx)
        self.assertEqual(doc["installed"]["package"], "org.fdroid.fdroid")
        self.assertIn("not Google Play", doc["installed"]["store"])
        cache = Path(self.tmp) / "home/.cache/boswas/android"
        self.assertEqual(list(cache.iterdir()), [])                  # download removed afterwards

    def test_wrong_certificate_is_refused_and_deleted(self):
        ctx, runner = self.ctx_with(self.fdroid_apk(), "0" * 64)
        with self.assertRaises(Refused) as caught:
            runtime.fdroid_install(ctx)
        self.assertEqual(caught.exception.code, "UNTRUSTED")
        self.assertFalse([c for c in runner.calls if c[1:3] == ["app", "install"]])
        self.assertEqual(list((Path(self.tmp) / "home/.cache/boswas/android").iterdir()), [])

    def test_http_url_is_refused(self):
        ctx, _ = self.ctx_with(self.fdroid_apk(), "0" * 64, url="http://f-droid.org/F-Droid.apk")
        with self.assertRaises(Failed):                               # invalid runtime facts
            runtime.fdroid_install(ctx)


class CleanupAndLogsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = fx.tempdir(self)

    def test_refused_while_runtime_installed(self):
        ctx = fx.make_ctx(self.tmp, fx.make_root(self.tmp, installed=True))
        with self.assertRaises(Refused):
            runtime.cleanup_user(ctx)

    def test_removes_only_waydroid_entries_and_data(self):
        ctx = fx.make_ctx(self.tmp, fx.make_root(self.tmp))
        apps = Path(self.tmp) / "home/.local/share/applications"
        apps.mkdir(parents=True)
        (apps / "waydroid.com.example.app.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=App\nExec=waydroid app launch com.example.app\n")
        (apps / "waydroid.com.example.other.desktop").write_text("[Desktop Entry]\nExec=/usr/bin/other\n")
        (apps / "org.kde.dolphin.desktop").write_text("[Desktop Entry]\nExec=dolphin\n")
        outside = Path(self.tmp) / "outside.desktop"
        outside.write_text("[Desktop Entry]\nExec=waydroid app launch com.evil.x\n")
        os.symlink(outside, apps / "waydroid.com.evil.x.desktop")
        data = Path(self.tmp) / "home/.local/share/waydroid/data"
        data.mkdir(parents=True)
        (data / "f").write_bytes(b"1")
        doc = runtime.cleanup_user(ctx)
        self.assertEqual(doc["cleanup"]["desktop_entries_removed"], 1)
        self.assertTrue(doc["cleanup"]["user_data_removed"])
        self.assertEqual(sorted(p.name for p in apps.iterdir()),
                         ["org.kde.dolphin.desktop", "waydroid.com.evil.x.desktop", "waydroid.com.example.other.desktop"])
        self.assertTrue(outside.exists())
        self.assertFalse((Path(self.tmp) / "home/.local/share/waydroid").exists())

    def test_logs(self):
        ctx = fx.make_ctx(self.tmp, fx.make_root(self.tmp, installed=True, initialized=True))
        from boswas_android import userlog
        userlog.write(ctx.host, "test-action", "OK", "detail\nwith newline")
        doc = runtime.logs(ctx, 10)
        self.assertEqual(len(doc["boswas"]["lines"]), 1)
        self.assertIn("test-action OK detail with newline", doc["boswas"]["lines"][0])
        self.assertEqual(doc["waydroid"]["lines"], ["line one", "line two"])
        log = Path(userlog.path(ctx.host))
        self.assertEqual(oct(log.stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
