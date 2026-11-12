"""Long operations: the JSON Lines protocol, what an outcome means, and the real streaming runner (no Qt).

The protocol is the contract of docs/architecture/platform-contracts.md
(section 1). The streaming runner is exercised with the Python interpreter
standing in for boswas-sysops: it prints JSON Lines and reads "cancel" from
its standard input.

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402

from boswas_control_center import commands, jobs  # noqa: E402
from boswas_control_center.errors import CommandFailed  # noqa: E402


def state_of(lines: list[str]) -> jobs.JobState:
    state = jobs.JobState()
    for line in lines:
        state.feed(jobs.parse_line(line))
    return state


class ParseTest(unittest.TestCase):
    def test_contract_lines(self):
        start = jobs.parse_line('{"type": "start", "schema": "boswas-sysops/1", "operation": "apt-install", '
                                '"cancellable": true}')
        self.assertEqual((start.kind, start.cancellable), ("start", True))
        progress = jobs.parse_line('{"type": "progress", "phase": "download", "percent": 42.5, "message": "x"}')
        self.assertEqual((progress.kind, progress.phase, progress.percent), ("progress", "download", 42.5))
        self.assertIsNone(jobs.parse_line('{"type": "progress", "phase": "install", "percent": 140}').percent)
        self.assertIsNone(jobs.parse_line('{"type": "progress", "percent": true}').percent)
        log = jobs.parse_line('{"type": "log", "level": "warning", "message": "careful"}')
        self.assertEqual((log.kind, log.level), ("log", "warning"))
        result = jobs.parse_line('{"type": "result", "ok": false, "code": "LOCKED", "message": "busy", '
                                 '"data": {"holder": {"pid": 12}}}')
        self.assertEqual((result.kind, result.ok, result.code, result.data["holder"]["pid"]),
                         ("result", False, "LOCKED", 12))

    def test_anything_else_is_text(self):
        for line in ("Error executing command as another user: Not authorized", "{not json", "[1, 2]", "",
                     '{"type": "unknown"}', "\x1b[31mred\x1b[0m"):
            self.assertEqual(jobs.parse_line(line).kind, "text", line)
        self.assertEqual(jobs.parse_line("\x1b[31mred\x1b[0m").message, "red")
        self.assertEqual(jobs.parse_line('{"schema": "boswas-android/1", "ok": true}').kind, "document")

    def test_cancellable_follows_the_phases(self):
        state = jobs.JobState()
        state.feed(jobs.parse_line('{"type": "start", "operation": "x", "cancellable": true}'))
        self.assertTrue(state.cancellable)
        state.feed(jobs.parse_line('{"type": "progress", "phase": "install", "percent": null, "cancellable": false}'))
        self.assertFalse(state.cancellable)
        self.assertEqual(state.progress_text, "Installing")
        state.feed(jobs.parse_line('{"type": "progress", "phase": "configure", "percent": 80}'))
        self.assertFalse(state.cancellable)                # unchanged without the field
        self.assertEqual(state.progress_text, "Configuring (80%)")
        state.feed(jobs.parse_line('{"type": "result", "ok": true, "code": "OK", "message": "m", "data": {}}'))
        self.assertFalse(state.cancellable)


class OutcomeTest(unittest.TestCase):
    def test_success_needs_ok_result_and_exit_zero(self):
        lines = fx.sysops_lines("apt-upgrade", message="3 packages upgraded.", data={"upgraded": ["a"]})
        outcome = jobs.outcome(state_of(lines), 0)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message, "3 packages upgraded.")
        self.assertEqual(outcome.data, {"upgraded": ["a"]})
        mismatch = jobs.outcome(state_of(lines), 1)
        self.assertFalse(mismatch.ok)
        self.assertEqual(mismatch.code, jobs.MALFORMED)

    def test_contract_failures(self):
        lines = fx.sysops_lines("apt-install", ok=False, code="LOCKED", message="Another package manager is running.")
        outcome = jobs.outcome(state_of(lines), 3)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.retryable)
        self.assertIn("Wait for it to finish", outcome.message)
        network = jobs.outcome(state_of(fx.sysops_lines("apt-refresh", ok=False, code="NETWORK", message="No network.",
                                                        data={"detail": "Could not resolve deb.debian.org"})), 5)
        self.assertTrue(network.retryable)
        self.assertEqual(network.detail, "Could not resolve deb.debian.org")
        for code in ("NOT_ALLOWED", "NOT_FOUND", "UNTRUSTED", "FAILED", "INVALID_ARGUMENT", "UNSUPPORTED"):
            outcome = jobs.outcome(state_of(fx.sysops_lines("apt-install", ok=False, code=code, message="No.")), 2)
            self.assertFalse(outcome.ok or outcome.retryable, code)
        cancelled = jobs.outcome(state_of(fx.sysops_lines("apt-refresh", ok=False, code="CANCELLED", message="")), 4)
        self.assertEqual(cancelled.message, jobs.CODE_HINTS["CANCELLED"])

    def test_authorization_denied(self):
        for code in (126, 127):
            outcome = jobs.outcome(state_of(["Error executing command as another user: Not authorized"]), code)
            self.assertTrue(outcome.auth_denied)
            self.assertEqual(outcome.message, jobs.AUTH_MESSAGE)
            self.assertIn("Not authorized", outcome.detail)

    def test_no_result_is_never_success(self):
        for lines, code in (([], 0), (["random output"], 0), (['{"type": "start", "cancellable": true}'], 0),
                            (['{"type": "progress", "phase": "download", "percent": 100}'], 1), ([], None)):
            outcome = jobs.outcome(state_of(lines), code)
            self.assertFalse(outcome.ok, (lines, code))
            self.assertEqual(outcome.code, jobs.MALFORMED)

    def test_broken_results(self):
        two = fx.sysops_lines("apt-clean") + [json.dumps({"type": "result", "ok": True, "code": "OK", "message": "",
                                                          "data": {}})]
        self.assertEqual(jobs.outcome(state_of(two), 0).code, jobs.MALFORMED)
        unknown = ['{"type": "result", "ok": true, "code": "PROBABLY", "message": "", "data": {}}']
        self.assertFalse(jobs.outcome(state_of(unknown), 0).ok)
        no_ok = ['{"type": "result", "code": "OK", "message": "", "data": {}}']
        self.assertFalse(jobs.outcome(state_of(no_ok), 0).ok)

    def test_android_documents(self):
        good = [json.dumps({"schema": "boswas-android/1", "ok": True, "message": "installed"})]
        self.assertTrue(jobs.outcome(state_of(good), 0, jobs.ANDROID).ok)
        self.assertFalse(jobs.outcome(state_of(good), 0, jobs.SYSOPS).ok)    # sysops needs a result line
        bad = [json.dumps({"schema": "boswas-android/1", "ok": False, "error": "session not running"})]
        outcome = jobs.outcome(state_of(bad), 1, jobs.ANDROID)
        self.assertFalse(outcome.ok)
        self.assertIn("Session not running", outcome.message)
        passthrough = fx.sysops_lines("android-init")
        self.assertTrue(jobs.outcome(state_of(passthrough), 0, jobs.ANDROID).ok)

    def test_android_documents_as_boswas_android_prints_them(self):
        installed = [json.dumps({"schema": "boswas-android/1", "installed": {
            "package": "org.example.notes", "label": "Example Notes", "message": "Example Notes was installed."}})]
        outcome = jobs.outcome(state_of(installed), 0, jobs.ANDROID)
        self.assertEqual((outcome.ok, outcome.message), (True, "Example Notes was installed."))
        removed = [json.dumps({"schema": "boswas-android/1", "removed": {"package": "org.example.notes"}})]
        self.assertEqual(jobs.outcome(state_of(removed), 0, jobs.ANDROID).message, "org.example.notes removed.")
        error = [json.dumps({"schema": "boswas-android/1", "error": {
            "code": "INCOMPATIBLE", "message": "this app cannot run here",
            "problems": ["needs an ARM translation layer"]}})]
        outcome = jobs.outcome(state_of(error), 6, jobs.ANDROID)
        self.assertEqual((outcome.ok, outcome.code), (False, "INCOMPATIBLE"))
        self.assertEqual(outcome.message, "This app cannot run here")
        self.assertIn("ARM translation layer", outcome.detail)
        # An error document is a failure even with exit status 0.
        self.assertFalse(jobs.outcome(state_of(error), 0, jobs.ANDROID).ok)


HELPER = r"""
import json, sys, time
def out(**doc):
    sys.stdout.write(json.dumps(doc) + "\n"); sys.stdout.flush()
mode = sys.argv[1]
out(type="start", schema="boswas-sysops/1", operation="apt-refresh", cancellable=True)
if mode == "wait-cancel":
    out(type="progress", phase="download", percent=10, message="downloading", cancellable=True)
    line = sys.stdin.readline()
    if line.strip() == "cancel":
        out(type="result", ok=False, code="CANCELLED", message="Cancelled.", data={})
        sys.exit(4)
    out(type="result", ok=True, code="OK", message="eof", data={})
elif mode == "stderr":
    sys.stderr.write("pkexec: not authorized\n"); sys.stderr.flush()
    sys.exit(127)
elif mode == "slow":
    time.sleep(30)
else:
    for i in range(3):
        out(type="progress", phase="download", percent=i * 50, message="x", cancellable=True)
    out(type="result", ok=True, code="OK", message="Done.", data={"n": 3})
"""


class StreamTest(unittest.TestCase):
    """The real CommandRunner.stream() with the Python interpreter standing in as the helper."""

    def setUp(self):
        self.patch = mock.patch.object(commands, "ALLOWED_PATHS", frozenset({sys.executable}))
        self.patch.start()
        self.runner = commands.CommandRunner({"PATH": "/usr/bin:/bin"})

    def tearDown(self):
        self.patch.stop()

    def run_helper(self, mode: str, handle=None, timeout: float = 30, on_line=None):
        lines: list[str] = []
        handle = handle or commands.StreamHandle()
        code = self.runner.stream([sys.executable, "-c", HELPER, mode], on_line or lines.append, handle, timeout)
        return code, lines, handle

    def test_lines_arrive_in_order(self):
        code, lines, handle = self.run_helper("ok")
        self.assertEqual(code, 0)
        outcome = jobs.outcome(state_of(lines), code)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.data, {"n": 3})
        self.assertTrue(handle.finished)
        self.assertFalse(handle.cancel())                # nothing left to cancel

    def test_cancel_writes_to_stdin(self):
        handle = commands.StreamHandle()
        seen: list[str] = []

        def on_line(line: str) -> None:
            seen.append(line)
            if '"progress"' in line:
                threading.Thread(target=handle.cancel).start()

        code, _lines, _h = self.run_helper("wait-cancel", handle, on_line=on_line)
        self.assertEqual(code, 4)
        self.assertTrue(handle.cancel_requested)
        self.assertEqual(jobs.outcome(state_of(seen), code).code, "CANCELLED")

    def test_stderr_is_read_as_text(self):
        code, lines, _h = self.run_helper("stderr")
        self.assertEqual(code, 127)
        self.assertIn("pkexec: not authorized", lines)
        self.assertTrue(jobs.outcome(state_of(lines), code).auth_denied)

    def test_timeout_kills(self):
        start = time.monotonic()
        with self.assertRaises(CommandFailed):
            self.run_helper("slow", timeout=0.5)
        self.assertLess(time.monotonic() - start, 15)

    def test_kill(self):
        handle = commands.StreamHandle()
        handle.kill()                                    # killed before the program even started
        start = time.monotonic()
        self.run_helper("slow", handle)
        self.assertLess(time.monotonic() - start, 15)
        self.assertTrue(handle.killed)

    def test_refuses_what_run_refuses(self):
        for command in (["/bin/sh", "-c", "true"], [], [sys.executable, 3]):
            with self.assertRaises(ValueError):
                self.runner.stream(command, print, commands.StreamHandle(), 5)
        with self.assertRaises(ValueError):
            self.runner.stream([sys.executable, "-c", "1"], print, commands.StreamHandle(), 0)


if __name__ == "__main__":
    unittest.main()
