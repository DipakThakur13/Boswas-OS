"""The protocol (JSON Lines, exactly one result, last), exit statuses, the privilege check and the audit log."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sysops_fixtures import Harness  # noqa: E402

from boswas_sysops import SCHEMA, audit, cli  # noqa: E402
from boswas_sysops.protocol import EXIT_CODES, Emitter, SysopsError  # noqa: E402

ENV = {"PKEXEC_UID": "1000"}


def run(h: Harness, *argv: str, euid: int = 0, environ=None) -> int:
    return cli.main(list(argv), ctx=h.ctx, euid=euid, environ=ENV if environ is None else environ, stdin=False)


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def assert_protocol(self, operation: str | None = None) -> dict:
        events = self.h.events()
        results = [e for e in events if e["type"] == "result"]
        self.assertEqual(len(results), 1, events)
        self.assertEqual(events[-1]["type"], "result")
        if operation:
            self.assertEqual(events[0], {"type": "start", "schema": SCHEMA, "operation": operation,
                                         "cancellable": cli.OPERATIONS[operation].cancellable})
        for event in events:
            self.assertIn(event["type"], {"start", "progress", "log", "result"})
            if event["type"] == "progress":
                self.assertIn(event["phase"], {"refresh", "download", "install", "configure", "remove", "cleanup",
                                               "verify", "init", "service", "inspect"})
                self.assertTrue(event["percent"] is None or 0 <= event["percent"] <= 100)
        result = results[0]
        self.assertEqual(set(result), {"type", "ok", "code", "message", "data"})
        self.assertIsInstance(result["data"], dict)
        self.assertTrue(result["message"])
        return result

    def test_every_line_is_one_json_object(self):
        run(self.h, "apt-clean")
        for line in self.h.buffer.getvalue().splitlines():
            self.assertIsInstance(json.loads(line), dict)

    def test_success(self):
        self.assertEqual(run(self.h, "apt-clean"), 0)
        result = self.assert_protocol("apt-clean")
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "OK")
        self.assertIn("freed_bytes", result["data"])

    def test_not_root(self):
        self.assertEqual(run(self.h, "apt-clean", euid=1000), 2)
        result = self.assert_protocol()
        self.assertEqual(result["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.h.runner.calls, [])

    def test_not_started_through_pkexec(self):
        self.assertEqual(run(self.h, "apt-clean", environ={}), 2)
        self.assertEqual(self.assert_protocol()["code"], "INVALID_ARGUMENT")
        self.assertEqual(run(Harness(), "apt-clean", environ={"PKEXEC_UID": "abc"}), 2)

    def test_unknown_operation_and_no_operation(self):
        for argv in (["rm", "-rf", "/"], [], ["apt-get"], ["--help"]):
            h = Harness()
            self.assertEqual(run(h, *argv), 2)
            events = h.events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["code"], "INVALID_ARGUMENT")
            self.assertEqual(h.runner.calls, [])
            h.close()

    def test_bad_arguments(self):
        self.assertEqual(run(self.h, "apt-clean", "extra"), 2)
        self.assertEqual(self.assert_protocol("apt-clean")["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.h.runner.calls, [])

    def test_unexpected_exception_is_a_failed_result(self):
        def boom(ctx, args):
            raise RuntimeError("secret internal detail " * 100)
        original = cli.OPERATIONS["apt-clean"]
        cli.OPERATIONS["apt-clean"] = cli.Operation(boom, False, False, "d", "m")
        try:
            self.assertEqual(run(self.h, "apt-clean"), 1)
        finally:
            cli.OPERATIONS["apt-clean"] = original
        result = self.assert_protocol()
        self.assertEqual(result["code"], "FAILED")
        self.assertNotIn("Traceback", self.h.buffer.getvalue())
        self.assertLessEqual(len(result["data"]["detail"]), 500)

    def test_exit_codes_match_the_contract(self):
        self.assertEqual(EXIT_CODES, {"OK": 0, "FAILED": 1, "DISK_SPACE": 1, "INVALID_ARGUMENT": 2, "NOT_ALLOWED": 2,
                                      "NOT_FOUND": 2, "UNTRUSTED": 2, "LOCKED": 3, "CANCELLED": 4, "NETWORK": 5,
                                      "UNSUPPORTED": 6})
        for code, status in EXIT_CODES.items():
            if code == "OK":
                continue

            def fail(ctx, args, code=code):
                raise SysopsError(code, "x")
            h = Harness()
            original = cli.OPERATIONS["apt-clean"]
            cli.OPERATIONS["apt-clean"] = cli.Operation(fail, False, False, "d", "m")
            try:
                self.assertEqual(run(h, "apt-clean"), status, code)
            finally:
                cli.OPERATIONS["apt-clean"] = original
            self.assertEqual(h.events()[-1]["code"], code)
            h.close()

    def test_no_event_after_the_result(self):
        emitter = Emitter(io.StringIO())
        emitter.result(True, "OK", "done")
        with self.assertRaises(RuntimeError):
            emitter.log("late")
        with self.assertRaises(ValueError):
            Emitter(io.StringIO()).result(True, "FAILED", "inconsistent")
        with self.assertRaises(ValueError):
            Emitter(io.StringIO()).progress("dance", 1, "")

    def test_broken_pipe_does_not_stop_the_operation(self):
        class Closed(io.StringIO):
            def write(self, text):
                raise BrokenPipeError()
        emitter = Emitter(Closed())
        emitter.start("apt-clean", False)
        emitter.progress("cleanup", 10, "x")
        emitter.result(True, "OK", "done")
        self.assertTrue(emitter.broken)

    def test_messages_are_one_line(self):
        buffer = io.StringIO()
        Emitter(buffer).result(False, "FAILED", "first line" + chr(10) + "second line" + chr(11) + "third")
        event = json.loads(buffer.getvalue())
        self.assertNotIn("\n", event["message"])

    def test_cancellable_flags(self):
        cancellable = {name for name, op in cli.OPERATIONS.items() if op.cancellable}
        self.assertEqual(cancellable, {"apt-refresh", "apt-upgrade", "apt-install", "apt-install-local",
                                       "android-runtime-install", "android-init", "android-update"})
        read_only = {name for name, op in cli.OPERATIONS.items() if op.read_only}
        self.assertEqual(read_only, {"firmware-report", "android-service"})


class AuditTest(unittest.TestCase):
    def test_every_invocation_is_logged(self):
        h = Harness()
        for argv in (["apt-clean"], ["apt-install", "Bad Name"], ["nonsense"]):
            h.ctx.emit = Emitter(io.StringIO())
            run(h, *argv)
        log_path = h.path(audit.LOG_FILE)
        lines = Path(log_path).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ uid=1000 op=apt-clean args=\[\] code=OK$")
        self.assertIn('op=apt-install args=["Bad Name"] code=INVALID_ARGUMENT', lines[1])
        self.assertIn("op=nonsense", lines[2])
        self.assertEqual(stat.S_IMODE(os.stat(log_path).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(log_path)).st_mode), 0o750)
        h.close()

    def test_log_lines_cannot_be_forged(self):
        h = Harness()
        audit.record(h.root, 1000, "apt-install", ["x\n2026-01-01T00:00:00Z uid=0 op=fake"], "OK")
        text = Path(h.path(audit.LOG_FILE)).read_text(encoding="utf-8")
        self.assertEqual(len(text.splitlines()), 1)
        h.close()


if __name__ == "__main__":
    unittest.main()
