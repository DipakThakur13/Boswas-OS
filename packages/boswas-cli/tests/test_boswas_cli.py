"""Unit tests for the boswas CLI. Run: python3 -m unittest discover -s tests

All system state comes from a temporary fake root (BOSWAS_SYSROOT) and
external commands are stubbed, so the tests never inspect the build host.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from boswas_cli import checks, main, system  # noqa: E402

RELEASE = """\
# comment
BOSWAS_NAME="Boswas OS"
BOSWAS_VERSION="v1 Alpha"
BOSWAS_VERSION_ID="1.0~alpha1"
BOSWAS_CHANNEL="dev"  # trailing comment
"""


def fake_run(cmd, timeout=10.0):
    table = {
        ("dpkg", "--print-architecture"): (0, "amd64"),
        ("systemctl", "is-active", "nftables.service"): (0, "active"),
        ("systemctl", "is-active", "auditd.service"): (0, "active"),
        ("systemctl", "is-active", "apparmor.service"): (0, "active"),
        ("apt-config", "shell", "UU", "APT::Periodic::Unattended-Upgrade"): (0, "UU='1'"),
    }
    if cmd[0] == "dpkg-query" and cmd[-1] == "plasma-workspace":
        return 0, "installed 4:6.3.6-2"
    return table.get(tuple(cmd), (127, ""))


class FakeRoot:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def write(self, path, content):
        p = self.root / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)
        return p

    def mkdir(self, path):
        (self.root / path.lstrip("/")).mkdir(parents=True, exist_ok=True)

    def cleanup(self):
        self._tmp.cleanup()


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.fs = FakeRoot()
        self.addCleanup(self.fs.cleanup)
        patcher = mock.patch.dict(os.environ, {"BOSWAS_SYSROOT": str(self.fs.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        run_patch = mock.patch.object(system, "run", side_effect=fake_run)
        run_patch.start()
        self.addCleanup(run_patch.stop)
        self.fs.write("/usr/lib/boswas/release", RELEASE)
        self.fs.write("/etc/os-release", 'NAME="Debian GNU/Linux"\nVERSION_CODENAME=trixie\n')
        self.fs.write("/etc/debian_version", "13.7\n")
        self.fs.write("/proc/cmdline", "BOOT_IMAGE=/vmlinuz root=/dev/mapper/vg-root ro quiet\n")
        self.fs.mkdir("/run/systemd/system")

    def run_cli(self, *argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def installed_luks2_on_lvm(self):
        self.fs.write("/proc/self/mountinfo",
                      "25 1 253:2 / / rw,relatime shared:1 - ext4 /dev/mapper/vg-root rw\n")
        self.fs.write("/sys/dev/block/253:2/dm/uuid", "LVM-abcdef\n")
        self.fs.write("/sys/dev/block/253:2/slaves/dm-0/dm/uuid", "CRYPT-LUKS2-0123-sda3_crypt\n")


class ParseEnvTests(CliTestCase):
    def test_quotes_comments_and_invalid_lines(self):
        parsed = system.parse_env(RELEASE + "not a pair\n1BAD=x\nEMPTY=\n")
        self.assertEqual(parsed["BOSWAS_NAME"], "Boswas OS")
        self.assertEqual(parsed["BOSWAS_CHANNEL"], "dev")
        self.assertEqual(parsed["EMPTY"], "")
        self.assertNotIn("1BAD", parsed)

    def test_upstream_version(self):
        self.assertEqual(system.upstream_version("4:6.3.6-2"), "6.3.6")
        self.assertEqual(system.upstream_version("1.0"), "1.0")
        self.assertIsNone(system.upstream_version(None))


class CheckTests(CliTestCase):
    def test_disk_encryption_luks2_on_lvm(self):
        self.installed_luks2_on_lvm()
        result = checks.disk_encryption()
        self.assertEqual(result.status, checks.PASS)
        self.assertIn("LUKS2", result.detail)

    def test_disk_encryption_luks1_warns(self):
        self.installed_luks2_on_lvm()
        self.fs.write("/sys/dev/block/253:2/slaves/dm-0/dm/uuid", "CRYPT-LUKS1-0123-sda3_crypt\n")
        self.assertEqual(checks.disk_encryption().status, checks.WARN)

    def test_disk_encryption_plain_partition_fails(self):
        self.fs.write("/proc/self/mountinfo", "25 1 8:2 / / rw - ext4 /dev/sda2 rw\n")
        self.fs.mkdir("/sys/dev/block/8:2")
        self.assertEqual(checks.disk_encryption().status, checks.FAIL)

    def test_live_session_is_informational(self):
        self.fs.mkdir("/run/live")
        result = checks.disk_encryption()
        self.assertEqual(result.status, checks.INFO)
        self.assertFalse(result.scored)

    def test_secure_boot_states(self):
        self.assertEqual(checks.secure_boot().status, checks.WARN)  # no /sys/firmware/efi: BIOS
        self.fs.write(checks.SECURE_BOOT_VAR, b"\x06\x00\x00\x00\x01")
        self.assertEqual(checks.secure_boot().status, checks.PASS)
        self.fs.write(checks.SECURE_BOOT_VAR, b"\x06\x00\x00\x00\x00")
        self.assertEqual(checks.secure_boot().status, checks.WARN)

    def test_screen_lock_reads_kiosk_keys(self):
        self.fs.write(f"{checks.KDE_SETTINGS}/kscreenlockerrc",
                      "[Daemon]\nAutolock[$i]=true\nTimeout[$i]=10\n")
        result = checks.screen_lock()
        self.assertEqual(result.status, checks.PASS)
        self.assertIn("10 min", result.detail)

    def test_root_account_requires_root(self):
        with mock.patch.object(system, "is_root", return_value=False):
            self.assertEqual(checks.root_account().status, checks.UNKNOWN)
        self.fs.write("/etc/shadow", "root:*:20000:0:99999:7:::\n")
        with mock.patch.object(system, "is_root", return_value=True):
            result = checks.root_account()
        self.assertEqual(result.status, checks.PASS)
        self.assertNotIn("*", result.detail)

    def test_apparmor_requires_loaded_profiles(self):
        self.fs.write("/sys/module/apparmor/parameters/enabled", "Y\n")
        self.assertEqual(checks.apparmor().status, checks.PASS)
        inactive = lambda cmd, timeout=10.0: (3, "inactive") if cmd[-1] == "apparmor.service" else fake_run(cmd)  # noqa: E731
        with mock.patch.object(system, "run", side_effect=inactive):
            self.assertEqual(checks.apparmor().status, checks.FAIL)      # installed: profiles missing
            self.fs.mkdir("/run/live")
            result = checks.apparmor()
        self.assertEqual(result.status, checks.WARN)                     # live media: Debian skips profiles
        self.assertIn("live session", result.detail)

    def test_apt_trust(self):
        self.fs.write("/etc/apt/sources.list", "deb http://deb.debian.org/debian trixie main\n"
                                               "# deb [trusted=yes] file:/old ./\n")
        self.assertEqual(checks.apt_trust().status, checks.PASS)
        self.fs.write("/etc/apt/sources.list.d/x.sources", "Types: deb\nURIs: file:/x\nTrusted: yes\n")
        self.assertEqual(checks.apt_trust().status, checks.FAIL)
        self.fs.write("/etc/apt/sources.list.d/x.sources", "")
        self.fs.write("/etc/apt/sources.list", "deb [trusted=yes] file:/run/live/medium trixie main\n")
        self.assertEqual(checks.apt_trust().status, checks.FAIL)         # installed system: never acceptable
        self.fs.mkdir("/run/live")
        result = checks.apt_trust()
        self.assertEqual(result.status, checks.INFO)                     # live session: boot medium pool
        self.assertFalse(result.scored)

    def test_compliance_states(self):
        mk = lambda s, scored=True: checks.Check("x", "security", "x", s, "", scored)  # noqa: E731
        self.assertEqual(checks.compliance([mk("PASS")])["state"], "COMPLIANT")
        self.assertEqual(checks.compliance([mk("PASS"), mk("WARN")])["state"], "COMPLIANT_WITH_WARNINGS")
        self.assertEqual(checks.compliance([mk("FAIL"), mk("INFO", False)])["state"], "NON_COMPLIANT")
        self.assertEqual(checks.compliance([mk("PASS"), mk("FAIL", False)])["state"], "COMPLIANT")

    def test_broken_probe_does_not_hide_others(self):
        def broken():
            raise RuntimeError("boom")
        results = checks.run_checks((broken, checks.tpm))
        self.assertEqual(results[0].status, checks.UNKNOWN)
        self.assertEqual(results[1].id, "tpm")


class CommandTests(CliTestCase):
    def test_info_text(self):
        code, out, _ = self.run_cli("info")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Boswas OS\n"))
        self.assertIn("v1 Alpha (1.0~alpha1)", out)
        self.assertIn("Debian 13.7", out)
        self.assertIn("trixie", out)
        self.assertIn("KDE Plasma 6.3.6", out)

    def test_info_json_global_and_trailing_flag(self):
        for argv in (("--json", "info"), ("info", "--json")):
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, 0)
            doc = json.loads(out)
            self.assertEqual(doc["schema"], "boswas-cli/1")
            self.assertEqual(doc["os"]["base"]["codename"], "trixie")
            self.assertEqual(doc["os"]["architecture"], "amd64")

    def test_status_exit_code_reflects_failures(self):
        self.installed_luks2_on_lvm()
        self.fs.write("/sys/module/apparmor/parameters/enabled", "Y\n")
        code, out, _ = self.run_cli("--json", "status")
        self.assertEqual(code, 0, out)
        self.assertNotEqual(json.loads(out)["compliance"]["state"], "NON_COMPLIANT")
        self.fs.write("/sys/module/apparmor/parameters/enabled", "N\n")
        code, out, _ = self.run_cli("--json", "status")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["compliance"]["state"], "NON_COMPLIANT")

    def test_device_status_never_shows_unknown_keys(self):
        self.fs.write("/etc/boswas/device.conf",
                      'DEVICE_ID=""\nENROLLMENT_STATE="unenrolled"\nENROLLMENT_TOKEN="s3cr3t"\n')
        code, out, _ = self.run_cli("--json", "device", "status")
        self.assertEqual(code, 0)
        self.assertNotIn("s3cr3t", out)
        self.assertEqual(json.loads(out)["device"]["enrollment_state"], "unenrolled")

    def test_planned_commands_are_unavailable(self):
        for argv in (("app", "list"), ("winapp", "list")):
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, 69)
            self.assertIn("not available", err)

    def test_usage_errors(self):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main.main(["no-such-command"])
        self.assertEqual(ctx.exception.code, 2)
        code, _, _ = self.run_cli()
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
