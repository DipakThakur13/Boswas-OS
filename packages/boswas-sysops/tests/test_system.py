"""Lock detection (real fcntl locks), the firmware report, journal cleanup, validation and the runner itself."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sysops_fixtures import Harness  # noqa: E402

from boswas_sysops import cli, firmware, locks, runner, validate  # noqa: E402
from boswas_sysops.protocol import SysopsError  # noqa: E402
from boswas_sysops.runner import Completed  # noqa: E402

ENV = {"PKEXEC_UID": "1000"}


def run(h: Harness, *argv: str) -> int:
    return cli.main(list(argv), ctx=h.ctx, euid=0, environ=ENV, stdin=False)


HOLD_LOCK = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o640)
fcntl.lockf(fd, fcntl.LOCK_EX)
print("locked", flush=True)
time.sleep(30)
"""


class LockTest(unittest.TestCase):
    def test_free_and_missing_lock_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lock")
            self.assertFalse(locks.is_locked(path))
            Path(path).write_text("")
            self.assertFalse(locks.is_locked(path))
            self.assertFalse(locks.is_locked(path), "the check releases its own lock")

    def test_lock_held_by_another_process(self):
        h = Harness()
        lock = h.write(locks.FRONTEND, "")
        holder = subprocess.Popen([sys.executable, "-c", HOLD_LOCK, lock], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            self.assertTrue(locks.is_locked(lock))
            st = os.stat(lock)
            ident = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
            h.write("/proc/locks", f"1: POSIX  ADVISORY  WRITE {holder.pid} {ident} 0 EOF\n"
                                   f"1: -> POSIX  ADVISORY  WRITE 999 {ident} 0 EOF\n")
            h.write(f"/proc/{holder.pid}/cmdline", "apt-get\0install\0vim\0")
            with self.assertRaises(SysopsError) as raised:
                locks.check(h.root, locks.FOR_PACKAGES)
            error = raised.exception
            self.assertEqual(error.code, "LOCKED")
            self.assertEqual(error.data["holder"], {"pid": holder.pid, "command": "apt-get install vim"})
            self.assertIn("apt-get install vim", error.message)
        finally:
            holder.kill()
            holder.wait()
        self.assertFalse(locks.is_locked(lock))
        h.close()

    def test_symlinked_lock_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            Path(target).write_text("")
            link = os.path.join(tmp, "lock")
            os.symlink(target, link)
            self.assertFalse(locks.is_locked(link))


JOURNAL = """\
Linux version 6.12.111+deb13-amd64 (debian-kernel@lists.debian.org)
r8169 0000:03:00.0: firmware: failed to load rtl_nic/rtl8125b-2.fw (-2)
r8169 0000:03:00.0: Direct firmware load for rtl_nic/rtl8125b-2.fw failed with error -2
r8169 0000:03:00.0: Unable to load firmware rtl_nic/rtl8125b-2.fw (-2)
iwlwifi 0000:00:14.3: Direct firmware load for iwlwifi-so-a0-gf-a0-89.ucode failed with error -2
iwlwifi 0000:00:14.3: firmware: failed to load iwl-debug-yoyo.bin (-2)
Bluetooth: hci0: Direct firmware load for rtl_bt/rtl8761bu_fw.bin failed with error -2
amdgpu 0000:05:00.0: firmware: failed to load amdgpu/../../etc/shadow (-2)
firmware_class: See https://wiki.debian.org/Firmware for information about missing firmware
"""


class FirmwareTest(unittest.TestCase):
    def test_parse(self):
        missing = firmware.parse_kernel_log(JOURNAL)
        self.assertEqual([m["file"] for m in missing],
                         ["rtl_nic/rtl8125b-2.fw", "iwlwifi-so-a0-gf-a0-89.ucode", "rtl_bt/rtl8761bu_fw.bin"])
        self.assertEqual(missing[0], {"file": "rtl_nic/rtl8125b-2.fw", "driver": "r8169", "device": "0000:03:00.0"})
        self.assertEqual(missing[2]["driver"], "Bluetooth")
        self.assertEqual(missing[2]["device"], "hci0")

    def test_dmesg_timestamps(self):
        missing = firmware.parse_kernel_log("[    3.141592] r8169 0000:03:00.0: firmware: failed to load "
                                            "rtl_nic/rtl8125b-2.fw (-2)\n")
        self.assertEqual(missing[0]["file"], "rtl_nic/rtl8125b-2.fw")

    def test_report_maps_files_to_packages(self):
        h = Harness(cancellable=False)
        h.write(firmware.INDEX, "rtl_nic/rtl8125b-2.fw\tfirmware-realtek\nrtl_bt/rtl8761bu_fw.bin\tfirmware-realtek\n"
                                "bad entry without tab\n../x\tevil\n")
        h.write("/usr/lib/firmware/iwlwifi-so-a0-gf-a0-89.ucode", b"\0")
        h.runner.on(("-k", "-b", "0"), Completed(0, JOURNAL, ""))
        h.runner.on(("-S",), Completed(0, "firmware-iwlwifi: /usr/lib/firmware/iwlwifi-so-a0-gf-a0-89.ucode\n", ""))
        h.runner.on(("policy",), Completed(0, "firmware-iwlwifi:\n  Installed: 20250410-2\n  Candidate: 20250410-2\n"
                                              "firmware-realtek:\n  Installed: (none)\n  Candidate: 20250410-2\n"
                                              "  Version table:\n     20250410-2 500\n", ""))
        self.assertEqual(run(h, "firmware-report"), 0)
        result = h.events()[-1]
        self.assertEqual(result["data"]["source"], "journal")
        by_file = {m["file"]: m for m in result["data"]["missing"]}
        self.assertEqual(by_file["rtl_nic/rtl8125b-2.fw"],
                         {"file": "rtl_nic/rtl8125b-2.fw", "driver": "r8169", "device": "0000:03:00.0",
                          "present": False, "package": "firmware-realtek", "installed": False,
                          "candidate": "20250410-2"})
        self.assertEqual(by_file["iwlwifi-so-a0-gf-a0-89.ucode"]["package"], "firmware-iwlwifi")
        self.assertTrue(by_file["iwlwifi-so-a0-gf-a0-89.ucode"]["installed"])
        self.assertTrue(by_file["iwlwifi-so-a0-gf-a0-89.ucode"]["present"])
        journal = h.runner.commands("run")[0]
        self.assertEqual(journal, ["/usr/bin/journalctl", "-k", "-b", "0", "-o", "cat", "--no-pager", "-q"])
        h.close()

    def test_dmesg_fallback_and_nothing_missing(self):
        h = Harness(cancellable=False)
        h.runner.on(("-k",), Completed(1, "", "No journal files were found."))
        h.runner.on(("--notime",), Completed(0, "Linux version 6.12\n", ""))
        self.assertEqual(run(h, "firmware-report"), 0)
        self.assertEqual(h.events()[-1]["data"], {"source": "dmesg", "missing": []})
        h.close()

    def test_no_kernel_log(self):
        h = Harness(cancellable=False)
        h.runner.on(("-k",), Completed(1, "", ""))
        h.runner.on(("--notime",), Completed(1, "", "dmesg: read kernel buffer failed: Operation not permitted"))
        self.assertEqual(run(h, "firmware-report"), 0)
        self.assertEqual(h.events()[-1]["data"]["source"], "none")
        h.close()


class JournalTest(unittest.TestCase):
    def test_vacuum(self):
        h = Harness(cancellable=False)
        old = h.write("/var/log/journal/abc/system@1.journal", b"j" * 50000)

        def vacuum(command):
            os.unlink(old)
            return Completed(0, "Vacuuming done, freed 48.0K of archived journals from /var/log/journal/abc.", "")
        h.runner.on(("--vacuum-size=200M",), vacuum)
        self.assertEqual(run(h, "journal-vacuum"), 0)
        self.assertGreaterEqual(h.events()[-1]["data"]["freed_bytes"], 50000)
        self.assertEqual(h.runner.commands()[0], ["/usr/bin/journalctl", "--vacuum-size=200M"])
        h.close()


class ValidateTest(unittest.TestCase):
    def test_package_names(self):
        for good in ("vim", "g++", "libstdc++6", "wine32:i386", "python3.13", "firmware-realtek", "a"):
            self.assertEqual(validate.packages([good]), [good])
        for bad in ("", "Vim", "-y", "--allow-unauthenticated", "vim;rm", "vim install", "../x", "x:arm64", "x" * 101,
                    "vim\n", "$(id)", "pkg=1.0", "pkg/stable", "*"):
            with self.assertRaises(SysopsError, msg=bad):
                validate.packages([bad])
        with self.assertRaises(SysopsError):
            validate.packages([])
        with self.assertRaises(SysopsError):
            validate.packages([f"p{i}" for i in range(51)])
        self.assertEqual(len(validate.packages([f"p{i}" for i in range(50)])), 50)

    def test_other_arguments(self):
        self.assertEqual(validate.remote_name(["flathub"]), "flathub")
        for bad in ([], ["a", "b"], ["Flathub"], ["../x"], ["-x"]):
            with self.assertRaises(SysopsError):
                validate.remote_name(bad)
        self.assertEqual(validate.service_verb(["restart"]), "restart")
        for bad in ([], ["enable"], ["start", "now"], ["mask"]):
            with self.assertRaises(SysopsError):
                validate.service_verb(bad)
        self.assertEqual(validate.remove_options(["--purge-data"]), ["--purge-data"])
        with self.assertRaises(SysopsError):
            validate.remove_options(["--purge-data", "/"])
        self.assertTrue(validate.firmware_file("rtl_nic/rtl8125b-2.fw"))
        self.assertTrue(validate.firmware_file("amdgpu/psp_13_0_0_sos.bin"))
        for bad in ("../etc/shadow", "a/../b", "/etc/passwd", "a b", ""):
            self.assertFalse(validate.firmware_file(bad), bad)


class StdinWatcherTest(unittest.TestCase):
    def test_cancel_line_from_a_pipe(self):
        from boswas_sysops.context import CancelRequest, watch_stdin
        read_fd, write_fd = os.pipe()
        cancel = CancelRequest()
        with os.fdopen(read_fd, "rb", buffering=0) as stream:
            watch_stdin(stream, cancel)
            os.write(write_fd, b"noise\ncanc")
            time.sleep(0.2)
            self.assertFalse(cancel.requested())
            os.write(write_fd, b"el\n")
            deadline = time.monotonic() + 5
            while not cancel.requested() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(cancel.requested())
            os.close(write_fd)

    def test_exit_status_survives_a_blocked_stdin_reader(self):
        """Regression: a daemon thread blocked in a buffered stdin read made Python abort at exit (status 134)."""
        code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "from boswas_sysops.context import CancelRequest, watch_stdin; "
                "watch_stdin(sys.stdin.buffer, CancelRequest()); sys.exit(4)")
        proc = subprocess.Popen([sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1])],
                                stdin=subprocess.PIPE)
        try:
            self.assertEqual(proc.wait(timeout=30), 4)
        finally:
            proc.stdin.close()


class RunnerTest(unittest.TestCase):
    """The real runner: allowlist, Status-Fd plumbing, \\r-separated progress, cancellation and timeout."""

    SCRIPT = """
import os, sys, time
fd = int(sys.argv[1].split("=", 1)[1])
os.write(fd, b"dlstatus:1:10.0:Retrieving file 1 of 2\\n")
sys.stdout.write("[Downloading] 1.00 MB/ 4.00 MB\\r[Downloading] 2.00 MB/ 4.00 MB\\r")
sys.stdout.write("Get:1 http://deb.debian.org trixie InRelease\\n")
sys.stdout.flush()
os.write(fd, b"pmstatus:vim:50.0:Unpacking vim\\n")
if len(sys.argv) > 2:
    time.sleep(float(sys.argv[2]))
sys.exit(int(os.environ.get("EXIT", "0")))
"""

    def setUp(self):
        self.saved = runner.ALLOWED_PATHS
        runner.ALLOWED_PATHS = frozenset(self.saved | {sys.executable})
        self.tmp = tempfile.TemporaryDirectory()
        self.script = os.path.join(self.tmp.name, "fake.py")
        Path(self.script).write_text(self.SCRIPT)

    def tearDown(self):
        runner.ALLOWED_PATHS = self.saved
        self.tmp.cleanup()

    def test_allowlist(self):
        runner.ALLOWED_PATHS = self.saved
        for command in (["/bin/sh", "-c", "id"], ["apt-get", "update"], [], "apt-get update", [sys.executable]):
            with self.assertRaises(ValueError):
                runner.Runner().run(command, 5)
        with self.assertRaises(ValueError):
            runner.argv("bash", "-c", "id")
        with self.assertRaises(ValueError):
            runner.argv("apt-get", "install", "a\nb")
        self.assertEqual(runner.argv("apt-get", "update"), ["/usr/bin/apt-get", "update"])
        for path in runner.EXECUTABLES.values():
            self.assertTrue(path.startswith(("/usr/bin/", "/usr/sbin/")), path)

    def test_stream_status_fd_and_lines(self):
        lines, status = [], []
        result = runner.Runner().stream([sys.executable, "-I", self.script, f"APT::Status-Fd={runner.STATUS_FD_TOKEN}"],
                                        timeout=30, on_line=lines.append, on_status=status.append)
        self.assertEqual(result.code, 0)
        self.assertFalse(result.cancelled)
        self.assertEqual(status, ["dlstatus:1:10.0:Retrieving file 1 of 2", "pmstatus:vim:50.0:Unpacking vim"])
        self.assertEqual(lines, ["[Downloading] 1.00 MB/ 4.00 MB", "[Downloading] 2.00 MB/ 4.00 MB",
                                 "Get:1 http://deb.debian.org trixie InRelease"])

    def test_environment_is_fixed(self):
        os.environ["BOSWAS_LEAK_TEST"] = "1"
        try:
            result = runner.Runner().run([sys.executable, "-I", "-c", "import os, json; print(json.dumps(dict(os.environ)))"],
                                         10)
        finally:
            del os.environ["BOSWAS_LEAK_TEST"]
        import json
        env = json.loads(result.stdout)
        self.assertNotIn("BOSWAS_LEAK_TEST", env)
        self.assertEqual(env["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")
        self.assertEqual(env["DEBIAN_FRONTEND"], "noninteractive")

    def test_cancel_stops_the_process_group(self):
        start = time.monotonic()
        polls = []

        def poll():
            polls.append(1)
            return len(polls) > 3
        result = runner.Runner().stream([sys.executable, "-I", self.script, f"x={runner.STATUS_FD_TOKEN}", "60"],
                                        timeout=120, on_status=lambda line: None, poll=poll)
        self.assertTrue(result.cancelled)
        self.assertNotEqual(result.code, 0)
        self.assertLess(time.monotonic() - start, 30)

    def test_timeout(self):
        result = runner.Runner().stream([sys.executable, "-I", self.script, f"x={runner.STATUS_FD_TOKEN}", "60"],
                                        timeout=1, on_status=lambda line: None)
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.code, 0)

    def test_placeholder_without_callback_is_refused(self):
        with self.assertRaises(ValueError):
            runner.Runner().stream([sys.executable, f"x={runner.STATUS_FD_TOKEN}"], timeout=5)

    def test_split_lines(self):
        lines, rest = runner._split_lines(b"a\rb\n\nc\r\nd")
        self.assertEqual(lines, ["a", "b", "c"])
        self.assertEqual(rest, b"d")


if __name__ == "__main__":
    unittest.main()
