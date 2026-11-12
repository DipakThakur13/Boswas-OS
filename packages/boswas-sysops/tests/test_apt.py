"""APT operations: argv shapes, Status-Fd progress, cancellation, error mapping, policy refusals, local .deb."""

from __future__ import annotations

import os
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sysops_fixtures import Harness  # noqa: E402

from boswas_sysops import apt, cli  # noqa: E402
from boswas_sysops.protocol import SysopsError  # noqa: E402
from boswas_sysops.runner import Completed  # noqa: E402

ENV = {"PKEXEC_UID": "1000"}
SIM_INSTALL = ("NOTE: This is only a simulation!\n"
               "Inst libfoo1 (1.2-1 Debian:13.7/stable [amd64])\n"
               "Inst firefox-esr [128.10-1] (128.11-1 Debian-Security:13/stable-security [amd64])\n"
               "Conf libfoo1 (1.2-1 Debian:13.7/stable [amd64])\n")


def run(h: Harness, *argv: str) -> int:
    return cli.main(list(argv), ctx=h.ctx, euid=0, environ=ENV, stdin=False)


class StatusFdTest(unittest.TestCase):
    def test_download_then_install_events(self):
        h = Harness()
        reporter = apt.AptReporter(h.ctx)
        reporter.status("dlstatus:1:5.0000:Retrieving file 1 of 3")
        reporter.status("dlstatus:2:55.5:Retrieving file 2 of 3: firefox-esr")
        reporter.status("pmstatus:firefox-esr:10.0:Preparing firefox-esr (amd64)")
        reporter.status("pmstatus:firefox-esr:60.0:Configuring firefox-esr (amd64)")
        reporter.status("pmerror:libbar:70:subprocess installed post-installation script returned error exit status 1")
        reporter.status("garbage")
        progress = h.kinds("progress")
        self.assertEqual([p["phase"] for p in progress], ["download", "download", "install", "configure"])
        self.assertEqual(progress[1]["percent"], 55.5)
        self.assertTrue(progress[0]["cancellable"])
        self.assertFalse(progress[2]["cancellable"])
        self.assertFalse(progress[3]["cancellable"])
        self.assertEqual(reporter.errors, ["subprocess installed post-installation script returned error exit status 1"])
        self.assertIn("warning", [e["level"] for e in h.kinds("log")])
        h.close()

    def test_descriptions_with_colons(self):
        h = Harness()
        apt.AptReporter(h.ctx).status("dlstatus:1:12.0:Retrieving file 1 of 2: http://deb.debian.org/x")
        self.assertEqual(h.kinds("progress")[0]["message"], "Retrieving file 1 of 2: http://deb.debian.org/x")
        h.close()

    def test_pm_phases(self):
        self.assertEqual(apt.pm_phase("Removing foo"), "remove")
        self.assertEqual(apt.pm_phase("Installed foo"), "configure")
        self.assertEqual(apt.pm_phase("Unpacking foo"), "install")


class CancellationTest(unittest.TestCase):
    def test_cancel_during_download_stops_apt(self):
        h = Harness()
        h.runner.on(("-s", "install"), Completed(0, SIM_INSTALL, ""))
        h.runner.script([("status", "dlstatus:1:10:Retrieving file 1 of 3"), ("cancel",),
                         ("status", "dlstatus:2:50:Retrieving file 2 of 3")])
        self.assertEqual(run(h, "apt-install", "firefox-esr"), 4)
        result = h.events()[-1]
        self.assertEqual(result["code"], "CANCELLED")
        self.assertIn("Nothing was changed", result["message"])
        h.close()

    def test_cancel_during_install_is_refused(self):
        h = Harness()
        h.runner.on(("-s", "install"), Completed(0, SIM_INSTALL, ""))
        h.runner.script([("status", "dlstatus:1:100:Retrieving file 3 of 3"),
                         ("status", "pmstatus:firefox-esr:20:Unpacking firefox-esr (amd64)"), ("cancel",),
                         ("status", "pmstatus:firefox-esr:90:Configuring firefox-esr (amd64)")])
        self.assertEqual(run(h, "apt-install", "firefox-esr"), 0)
        warnings = [e for e in h.kinds("log") if e["level"] == "warning"]
        self.assertTrue(any("cannot be cancelled" in w["message"] for w in warnings))
        self.assertEqual(h.events()[-1]["code"], "OK")
        h.close()

    def test_non_cancellable_operation_ignores_cancel(self):
        h = Harness(cancellable=False)
        reporter = apt.AptReporter(h.ctx)
        h.cancel.request()
        self.assertFalse(reporter.poll())
        self.assertFalse(h.cancel.requested())
        h.close()


class ErrorMappingTest(unittest.TestCase):
    def code(self, *lines: str) -> str:
        return apt.classify(list(lines), "Installing", 100).code

    def test_mapping(self):
        self.assertEqual(self.code("E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 4242 "
                                   "(apt-get)"), "LOCKED")
        self.assertEqual(self.code("E: You don't have enough free space in /var/cache/apt/archives/."), "DISK_SPACE")
        self.assertEqual(self.code("W: GPG error: http://x trixie InRelease: NO_PUBKEY 0123456789ABCDEF",
                                   "E: The repository 'http://x trixie InRelease' is not signed."), "UNTRUSTED")
        self.assertEqual(self.code("E: Unable to locate package nonexistent"), "NOT_FOUND")
        self.assertEqual(self.code("E: Package 'foo' has no installation candidate"), "NOT_FOUND")
        self.assertEqual(self.code("E: Failed to fetch http://deb.debian.org/x.deb  Temporary failure resolving "
                                   "'deb.debian.org'"), "NETWORK")
        self.assertEqual(self.code("W: Could not resolve 'deb.debian.org'"), "NETWORK")
        self.assertEqual(self.code("E: Sub-process /usr/bin/dpkg returned an error code (1)"), "FAILED")

    def test_lock_holder(self):
        error = apt.classify(["E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 4242 "
                              "(unattended-upgr)"], "Installing", 100)
        self.assertEqual(error.data["holder"], {"pid": 4242, "command": "unattended-upgr"})
        self.assertIn("unattended-upgr 4242", error.message)

    def test_not_found_names_the_package(self):
        error = apt.classify(["E: Unable to locate package frobnicate"], "Installing", 100)
        self.assertEqual(error.data["package"], "frobnicate")

    def test_failure_through_the_protocol(self):
        h = Harness()
        h.runner.on(("-s", "install"), Completed(0, SIM_INSTALL, ""))
        h.runner.script([("line", "E: Failed to fetch http://deb.debian.org/pool/x.deb  Could not resolve "
                                  "'deb.debian.org'")], code=100)
        self.assertEqual(run(h, "apt-install", "firefox-esr"), 5)
        result = h.events()[-1]
        self.assertEqual(result["code"], "NETWORK")
        self.assertIn("Could not resolve", result["data"]["detail"])
        h.close()

    def test_simulation_failure_not_found(self):
        h = Harness()
        h.runner.on(("-s", "install"), Completed(100, "", "E: Unable to locate package nosuch"))
        self.assertEqual(run(h, "apt-install", "nosuch"), 2)
        self.assertEqual(h.events()[-1]["code"], "NOT_FOUND")
        self.assertEqual(h.runner.commands("stream"), [])
        h.close()


class InstallTest(unittest.TestCase):
    def test_argv_and_result(self):
        h = Harness()
        h.runner.on(("-s", "install"), Completed(0, SIM_INSTALL, ""))
        h.runner.script([("status", "dlstatus:1:50:Retrieving"), ("status", "pmstatus:firefox-esr:50:Unpacking")])
        self.assertEqual(run(h, "apt-install", "firefox-esr", "firefox-esr"), 0)
        command = h.runner.commands("stream")[0]
        self.assertEqual(command[0], "/usr/bin/apt-get")
        self.assertIn("APT::Status-Fd=@STATUS_FD@", command)
        self.assertIn("APT::Get::AllowUnauthenticated=false", command)
        self.assertEqual(command[-3:], ["-y", "install", "firefox-esr"])
        for forbidden in ("--allow-unauthenticated", "--allow-downgrades", "--allow-remove-essential",
                          "--allow-change-held-packages", "--force-yes"):
            self.assertNotIn(forbidden, command)
        self.assertEqual([a for a in command if a.startswith("Dpkg::Options::")],
                         ["Dpkg::Options::=--force-confdef", "Dpkg::Options::=--force-confold"])
        result = h.events()[-1]
        self.assertEqual(result["data"]["installed"][1], {"package": "firefox-esr", "from": "128.10-1",
                                                          "to": "128.11-1"})
        h.close()

    def test_excluded_package_is_refused_before_anything_runs(self):
        h = Harness()
        self.assertEqual(run(h, "apt-install", "openssh-server"), 2)
        self.assertEqual(h.events()[-1]["code"], "NOT_ALLOWED")
        self.assertEqual(h.runner.calls, [])
        h.close()

    def test_excluded_dependency_is_refused(self):
        h = Harness()
        h.runner.on(("-s", "install"), Completed(0, "Inst kdeconnect (24.12 Debian:13 [amd64])\n"
                                                    "Inst some-tool (1.0 Debian:13 [amd64])\n", ""))
        self.assertEqual(run(h, "apt-install", "some-tool"), 2)
        result = h.events()[-1]
        self.assertEqual(result["code"], "NOT_ALLOWED")
        self.assertEqual(result["data"]["packages"], ["kdeconnect"])
        self.assertEqual(h.runner.commands("stream"), [])
        h.close()

    def test_install_that_removes_a_protected_package_is_refused(self):
        h = Harness()
        h.runner.on(("-s", "install"), Completed(0, "Remv sudo [1.9.16p2-3]\nInst sudo-ldap (1.9 Debian:13 [amd64])\n",
                                                 ""))
        self.assertEqual(run(h, "apt-install", "sudo-ldap"), 2)
        self.assertEqual(h.events()[-1]["code"], "NOT_ALLOWED")
        h.close()

    def test_already_installed(self):
        h = Harness()
        h.runner.on(("-s", "install"), Completed(0, "firefox-esr is already the newest version (128.11-1).\n", ""))
        self.assertEqual(run(h, "apt-install", "firefox-esr"), 0)
        self.assertEqual(h.runner.commands("stream"), [])
        self.assertEqual(h.events()[-1]["data"]["installed"], [])
        h.close()

    def test_lock_held_reports_locked_without_running_apt(self):
        h = Harness()
        h.runner.on(("-s", "install"), Completed(0, SIM_INSTALL, ""))
        original = apt.locks.is_locked
        apt.locks.is_locked = lambda path: path.endswith("lock-frontend")
        try:
            self.assertEqual(run(h, "apt-install", "firefox-esr"), 3)
        finally:
            apt.locks.is_locked = original
        self.assertEqual(h.events()[-1]["code"], "LOCKED")
        self.assertEqual(h.runner.commands("stream"), [])
        h.close()


class RemoveTest(unittest.TestCase):
    def test_protected_packages_are_refused(self):
        for removed in ("boswas-os", "linux-image-amd64", "grub-efi-amd64-signed", "shim-signed", "cryptsetup-bin",
                        "sudo", "apparmor", "network-manager", "dpkg", "plasma-workspace"):
            h = Harness()
            h.runner.on(("-s", "remove"), Completed(0, f"Remv some-app [1.0]\nRemv {removed} [1.0]\n", ""))
            self.assertEqual(run(h, "apt-remove", "some-app"), 2, removed)
            result = h.events()[-1]
            self.assertEqual(result["code"], "NOT_ALLOWED")
            self.assertIn(removed, result["data"]["packages"])
            self.assertEqual(h.runner.commands("stream"), [])
            h.close()

    def test_essential_and_required_are_refused(self):
        h = Harness()
        h.runner.on(("-s", "remove"), Completed(0, "Remv coreutils [9.7-3]\nRemv tool [1]\n", ""))
        h.runner.on(lambda c: c[0] == "/usr/bin/dpkg-query",
                    Completed(0, "coreutils\tyes\trequired\ntool\tno\toptional\n", ""))
        self.assertEqual(run(h, "apt-remove", "tool"), 2)
        self.assertEqual(h.events()[-1]["data"]["packages"], ["coreutils"])
        h.close()

    def test_remove_runs_apt(self):
        h = Harness()
        h.runner.on(("-s", "remove"), Completed(0, "Remv gnome-chess [1:48]\n", ""))
        h.runner.script([("status", "pmstatus:gnome-chess:50:Removing gnome-chess (amd64)")])
        self.assertEqual(run(h, "apt-remove", "gnome-chess"), 0)
        self.assertEqual(h.runner.commands("stream")[0][-3:], ["-y", "remove", "gnome-chess"])
        self.assertEqual(h.events()[-1]["data"]["removed"], ["gnome-chess"])
        self.assertEqual(h.kinds("progress")[-1]["phase"], "remove")
        h.close()

    def test_not_installed(self):
        h = Harness()
        h.runner.on(("-s", "remove"), Completed(0, "Package 'x' is not installed, so not removed\n", ""))
        self.assertEqual(run(h, "apt-remove", "x"), 0)
        self.assertEqual(h.runner.commands("stream"), [])
        h.close()


class RefreshUpgradeTest(unittest.TestCase):
    def test_refresh_ok(self):
        h = Harness()
        h.runner.script([("line", "Hit:1 http://deb.debian.org/debian trixie InRelease"),
                         ("status", "dlstatus:1:40:Retrieving file 1 of 2")])
        self.assertEqual(run(h, "apt-refresh"), 0)
        self.assertEqual(h.runner.commands("stream")[0][-1], "update")
        self.assertEqual(h.events()[-1]["data"], {"errors": []})
        self.assertEqual(h.kinds("progress")[-1]["phase"], "refresh")
        h.close()

    def test_refresh_partial_failure(self):
        h = Harness()
        h.runner.script([("line", "Hit:1 http://deb.debian.org/debian trixie InRelease"),
                         ("line", "Err:2 http://deb.debian.org/debian-security trixie-security InRelease"),
                         ("line", "  Temporary failure resolving 'deb.debian.org'"),
                         ("line", "W: Some index files failed to download. They have been ignored, or old ones "
                                  "used instead.")])
        self.assertEqual(run(h, "apt-refresh"), 0)
        result = h.events()[-1]
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["errors"], ["http://deb.debian.org/debian-security trixie-security InRelease"])
        h.close()

    def test_refresh_offline(self):
        h = Harness()
        h.runner.script([("line", "Err:1 http://deb.debian.org/debian trixie InRelease"),
                         ("line", "W: Failed to fetch http://deb.debian.org/debian/dists/trixie/InRelease  "
                                  "Temporary failure resolving 'deb.debian.org'")])
        self.assertEqual(run(h, "apt-refresh"), 5)
        self.assertEqual(h.events()[-1]["code"], "NETWORK")
        h.close()

    def test_upgrade_nothing_to_do(self):
        h = Harness()
        h.runner.on(("-s", "upgrade"), Completed(0, "0 upgraded, 0 newly installed, 0 to remove.\n", ""))
        self.assertEqual(run(h, "apt-upgrade"), 0)
        self.assertEqual(h.events()[-1]["data"], {"upgraded": []})
        self.assertEqual(h.runner.commands("stream"), [])
        h.close()

    def test_upgrade(self):
        h = Harness()
        h.runner.on(("-s", "upgrade"), Completed(0, SIM_INSTALL, ""))
        h.runner.script([])
        self.assertEqual(run(h, "apt-upgrade"), 0)
        self.assertEqual(h.runner.commands("stream")[0][-3:], ["-y", "--with-new-pkgs", "upgrade"])
        self.assertEqual(len(h.events()[-1]["data"]["upgraded"]), 2)
        for command in h.runner.commands():
            self.assertNotIn("dist-upgrade", command)
            self.assertNotIn("full-upgrade", command)
        h.close()

    def test_clean_reports_freed_bytes(self):
        h = Harness()
        deb = h.write("/var/cache/apt/archives/big_1.0_amd64.deb", b"x" * 100000)

        def clean(command):
            os.unlink(deb)
            return Completed(0, "", "")
        h.runner.on(("clean",), clean)
        self.assertEqual(run(h, "apt-clean"), 0)
        self.assertGreaterEqual(h.events()[-1]["data"]["freed_bytes"], 100000)
        h.close()


class LocalDebTest(unittest.TestCase):
    DEB = b"!<arch>\ndebian-binary   1700000000  0     0     100644  4         `\n2.0\n"

    def setUp(self):
        self.h = Harness(uid=os.getuid())
        self.h.runner.on(("--field",), Completed(0, "Package: hello\nVersion: 2.10-3\nArchitecture: amd64\n", ""))
        self.h.runner.on(("-s", "install"), Completed(0, "Inst hello (2.10-3 local-deb [amd64])\n", ""))
        self.deb = self.h.write("/home/user/Downloads/hello_2.10-3_amd64.deb", self.DEB)

    def tearDown(self):
        self.h.close()

    def test_installs_from_a_private_copy(self):
        seen = {}

        def inspect(command):
            copy = command[2]
            seen["copy"] = copy
            seen["mode"] = stat.S_IMODE(os.stat(os.path.dirname(copy)).st_mode)
            seen["content"] = Path(copy).read_bytes()
            return Completed(0, "Package: hello\nVersion: 2.10-3\nArchitecture: amd64\n", "")
        self.h.runner.handlers[0] = (("--field",), inspect)
        self.assertEqual(run(self.h, "apt-install-local", "/home/user/Downloads/hello_2.10-3_amd64.deb"), 0)
        self.assertTrue(seen["copy"].startswith(self.h.path("/var/cache/boswas/sysops/")))
        self.assertNotEqual(seen["copy"], self.deb)
        self.assertEqual(seen["mode"], 0o700)
        self.assertEqual(seen["content"], self.DEB)
        command = self.h.runner.commands("stream")[0]
        self.assertEqual(command[-1], seen["copy"])
        self.assertFalse(os.path.exists(seen["copy"]), "the private copy is removed afterwards")
        result = self.h.events()[-1]
        self.assertFalse(result["data"]["verified"])
        self.assertIn("not verified by a signed repository", result["message"])

    def test_policy_deny(self):
        self.h.write("/etc/boswas/apps.conf", 'LOCAL_DEB="deny"\n')
        self.assertEqual(run(self.h, "apt-install-local", "/home/user/Downloads/hello_2.10-3_amd64.deb"), 2)
        self.assertEqual(self.h.events()[-1]["code"], "NOT_ALLOWED")
        self.assertEqual(self.h.runner.calls, [])

    def test_unknown_policy_value_counts_as_deny(self):
        self.h.write("/etc/boswas/apps.conf", 'LOCAL_DEB="maybe"\n')
        self.assertEqual(run(self.h, "apt-install-local", "/home/user/Downloads/hello_2.10-3_amd64.deb"), 2)

    def test_path_validation(self):
        for bad in ("relative/x.deb", "/home/user/../root/x.deb", "/home/user/x.rpm", "/home/user//x.deb",
                    "/home/user/.deb", "/tmp/x.deb\n"):
            h = Harness()
            self.assertEqual(run(h, "apt-install-local", bad), 2, bad)
            self.assertEqual(h.events()[-1]["code"], "INVALID_ARGUMENT", bad)
            h.close()
        self.assertEqual(run(self.h, "apt-install-local", "/a.deb", "/b.deb"), 2)

    def test_symlink_is_refused(self):
        link = self.h.path("/home/user/Downloads/link.deb")
        os.symlink(self.deb, link)
        self.assertEqual(run(self.h, "apt-install-local", "/home/user/Downloads/link.deb"), 2)
        self.assertEqual(self.h.events()[-1]["code"], "INVALID_ARGUMENT")

    def test_not_a_deb(self):
        self.h.write("/home/user/fake.deb", b"#!/bin/sh\necho owned\n")
        self.assertEqual(run(self.h, "apt-install-local", "/home/user/fake.deb"), 2)
        self.assertEqual(self.h.events()[-1]["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.h.runner.calls, [])

    def test_directory_and_missing(self):
        os.makedirs(self.h.path("/home/user/dir.deb"))
        self.assertEqual(run(self.h, "apt-install-local", "/home/user/dir.deb"), 2)
        self.assertEqual(run(Harness(), "apt-install-local", "/nonexistent.deb"), 2)

    def test_file_of_another_user_is_refused(self):
        h = Harness(uid=1000)
        path = h.write("/home/other/x.deb", self.DEB)
        if os.geteuid() == 0:
            os.chown(path, 4242, 4242)          # as root: hand the file to another user
        elif os.stat(path).st_uid in (0, 1000):
            self.skipTest("cannot create a file owned by a third user")
        self.assertEqual(run(h, "apt-install-local", "/home/other/x.deb"), 2)
        self.assertEqual(h.events()[-1]["code"], "NOT_ALLOWED")
        self.assertEqual(h.runner.calls, [])
        with self.assertRaises(SysopsError) as raised:
            apt.secure_copy(h.ctx, "/home/other/x.deb")
        self.assertEqual(raised.exception.code, "NOT_ALLOWED")
        h.close()

    def test_file_of_the_requesting_user_is_accepted(self):
        h = Harness(uid=1000)
        path = h.write("/home/user/x.deb", self.DEB)
        if os.geteuid() == 0:
            os.chown(path, 1000, 1000)
        elif os.stat(path).st_uid != 1000:
            self.skipTest("cannot create a file owned by UID 1000")
        workdir, copy = apt.secure_copy(h.ctx, "/home/user/x.deb")
        self.assertEqual(Path(copy).read_bytes(), self.DEB)
        apt._remove_tree(workdir)
        h.close()

    def test_protected_package_name_is_refused(self):
        self.h.runner.handlers[0] = (("--field",), Completed(0, "Package: boswas-os\nVersion: 9\nArchitecture: all\n",
                                                              ""))
        self.assertEqual(run(self.h, "apt-install-local", "/home/user/Downloads/hello_2.10-3_amd64.deb"), 2)
        self.assertEqual(self.h.events()[-1]["code"], "NOT_ALLOWED")
        self.assertEqual(self.h.runner.commands("stream"), [])
        self.assertEqual(os.listdir(self.h.path("/var/cache/boswas/sysops")), [], "no copy left behind")


class SimulationParsingTest(unittest.TestCase):
    def test_parse(self):
        plan = apt.parse_simulation("Inst wine32:i386 (10.0~repack-6 Debian:13.7/stable [i386])\n"
                                    "Inst libc6 [2.41-11] (2.41-12 Debian:13.7/stable [amd64])\n"
                                    "Remv old-tool [0.9]\nPurg cfg-tool [1]\nConf libc6 (2.41-12)\n")
        self.assertEqual(plan.install[0], {"package": "wine32:i386", "from": None, "to": "10.0~repack-6"})
        self.assertEqual(plan.install[1]["from"], "2.41-11")
        self.assertEqual(plan.remove, ["old-tool", "cfg-tool"])


if __name__ == "__main__":
    unittest.main()
