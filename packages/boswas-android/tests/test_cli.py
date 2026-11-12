"""The command line: JSON documents, error documents, exit codes, sysops pass-through.

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import android_fixtures as fx  # noqa: E402

from boswas_android import apk, cli  # noqa: E402


def run_cli(argv, ctx):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO("")):
        code = cli.main(argv, ctx)
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = fx.tempdir(self)
        self.ctx = fx.make_ctx(self.tmp, fx.make_root(self.tmp))

    def apk(self, abis=("x86_64",)):
        return fx.build_apk(os.path.join(self.tmp, "a.apk"), fx.manifest(), abis,
                            pairs={apk.SIG_V2: fx.v2_value([b"\x30\x00"])})

    def test_status_json_flag_anywhere(self):
        for argv in (["--json", "status"], ["status", "--json"]):
            code, out, _ = run_cli(argv, self.ctx)
            self.assertEqual(code, 0)
            doc = json.loads(out)
            self.assertEqual(doc["schema"], "boswas-android/1")
            self.assertFalse(doc["google_play"]["available"])

    def test_status_text(self):
        code, out, _ = run_cli(["status"], self.ctx)
        self.assertEqual(code, 0)
        self.assertIn("Android runtime: not installed", out)
        self.assertIn("Google Play: not available", out)

    def test_check(self):
        code, out, _ = run_cli(["--json", "check"], self.ctx)
        doc = json.loads(out)
        self.assertEqual(set(doc), {"schema", "compatibility"})
        self.assertTrue(doc["compatibility"]["supported"])

    def test_apk_inspect(self):
        code, out, _ = run_cli(["--json", "apk", "inspect", self.apk()], self.ctx)
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["apk"]["package"], "com.boswas.testapp")
        code, out, _ = run_cli(["apk", "inspect", self.apk(("armeabi-v7a",))], self.ctx)
        self.assertEqual(code, 0)
        self.assertIn("Compatible: no", out)
        self.assertIn("does not show that the app is safe", out)

    def test_invalid_apk_error_document(self):
        bad = Path(self.tmp) / "bad.apk"
        bad.write_bytes(b"not a zip at all" * 10)
        code, out, err = run_cli(["--json", "apk", "inspect", str(bad)], self.ctx)
        self.assertEqual(code, 2)
        doc = json.loads(out)
        self.assertEqual(doc["schema"], "boswas-android/1")
        self.assertEqual(doc["error"]["code"], "INVALID_APK")
        self.assertNotIn("Traceback", out + err)
        code, _, err = run_cli(["apk", "inspect", str(bad)], self.ctx)
        self.assertEqual(code, 2)
        self.assertIn("boswas-android:", err)

    def test_incompatible_install_is_refused_with_problems(self):
        code, out, _ = run_cli(["--json", "apk", "install", self.apk(("arm64-v8a",))], self.ctx)
        self.assertEqual(code, 2)
        doc = json.loads(out)
        self.assertEqual(doc["error"]["code"], "INCOMPATIBLE")
        self.assertTrue(any("ARM" in p for p in doc["error"]["problems"]))

    def test_runtime_missing_exit_3(self):
        code, out, _ = run_cli(["--json", "app", "list"], self.ctx)
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["error"]["code"], "RUNTIME_NOT_INSTALLED")

    def test_google_play(self):
        code, out, _ = run_cli(["--json", "google-play"], self.ctx)
        doc = json.loads(out)["google_play"]
        self.assertFalse(doc["available"])
        self.assertTrue(doc["details"])

    def test_cleanup_needs_confirmation(self):
        code, out, _ = run_cli(["--json", "cleanup"], self.ctx)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"]["code"], "CONFIRMATION_REQUIRED")
        code, out, _ = run_cli(["--json", "cleanup", "--yes"], self.ctx)
        self.assertEqual(code, 0)

    def test_runtime_install_streams_sysops_json_lines(self):
        events = [{"type": "start", "schema": "boswas-sysops/1", "operation": "android-runtime-install",
                   "cancellable": True},
                  {"type": "result", "ok": False, "code": "NETWORK", "message": "No network.", "data": {}}]
        self.ctx.runner.stream_lines = [json.dumps(e) for e in events]
        self.ctx.runner.stream_code = 5
        code, out, _ = run_cli(["--json", "runtime", "install"], self.ctx)
        self.assertEqual(code, 5)
        self.assertEqual([json.loads(line) for line in out.splitlines()], events)
        self.assertIsNotNone(self.ctx.runner.cancel_events[0])

    def test_runtime_install_text(self):
        self.ctx.runner.stream_lines = [json.dumps({"type": "progress", "phase": "download", "percent": 12.0,
                                                    "message": "Downloading"}),
                                        json.dumps({"type": "result", "ok": True, "code": "OK", "message": "Installed.",
                                                    "data": {}})]
        code, out, _ = run_cli(["runtime", "install"], self.ctx)
        self.assertEqual(code, 0)
        self.assertIn("[download 12%] Downloading", out)
        self.assertIn("Done: Installed.", out)

    def test_runtime_install_unsupported_exit_6(self):
        ctx = fx.make_ctx(self.tmp, fx.make_root(fx.tempdir(self), binder=None))
        code, out, _ = run_cli(["--json", "runtime", "install"], ctx)
        self.assertEqual(code, 6)
        self.assertEqual(json.loads(out)["error"]["code"], "UNSUPPORTED")
        self.assertEqual(ctx.runner.streams, [])

    def test_internal_errors_have_no_traceback(self):
        ctx = fx.make_ctx(self.tmp, fx.make_root(fx.tempdir(self), installed=True))
        ctx.runner.answers[("is-active", "waydroid-container.service")] = ZeroDivisionError("boom")
        code, out, err = run_cli(["--json", "status"], ctx)
        self.assertEqual(code, 70)
        self.assertEqual(json.loads(out)["error"]["code"], "INTERNAL")
        self.assertNotIn("Traceback", out + err)

    def test_usage_errors_exit_2(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.main(["runtime", "format"], self.ctx)
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
