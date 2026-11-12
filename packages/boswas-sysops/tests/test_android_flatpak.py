"""Android runtime operations, the keyring fingerprint check, and Flatpak remotes."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sysops_fixtures import FLATHUB_FINGERPRINT, FLATHUB_REPO, Harness, flathub_key  # noqa: E402

from boswas_sysops import android, cli, flatpak, openpgp  # noqa: E402
from boswas_sysops.runner import Completed  # noqa: E402

ENV = {"PKEXEC_UID": "1000"}
CONF = """# runtime.conf (test copy)
REPO_URL="https://repo.waydro.id"
REPO_SUITE="trixie"
REPO_COMPONENTS="main"
KEY_FINGERPRINT="{fp}"
ALLOWED_PACKAGES="waydroid python3-gbinder libgbinder libglibutil"
IMAGE_TYPE="VANILLA"
FDROID_URL="https://f-droid.org/F-Droid.apk"
"""
FINGERPRINT_SPACED = " ".join(FLATHUB_FINGERPRINT[i:i + 4] for i in range(0, 40, 4))


def run(h: Harness, *argv: str) -> int:
    return cli.main(list(argv), ctx=h.ctx, euid=0, environ=ENV, stdin=False)


class OpenPgpTest(unittest.TestCase):
    def test_fingerprint_of_a_real_key(self):
        self.assertEqual(openpgp.primary_fingerprints(flathub_key()), [FLATHUB_FINGERPRINT])

    def test_armored_key(self):
        import base64
        body = base64.b64encode(flathub_key()).decode()
        lines = [body[i:i + 64] for i in range(0, len(body), 64)]
        armored = ("-----BEGIN PGP PUBLIC KEY BLOCK-----\nComment: test\n\n" + "\n".join(lines) +
                   "\n=abcd\n-----END PGP PUBLIC KEY BLOCK-----\n").encode()
        self.assertEqual(openpgp.primary_fingerprints(armored), [FLATHUB_FINGERPRINT])

    def test_malformed(self):
        for data in (b"\x00\x01\x02", b"\x99\x00", b"\xc6\xff\x00\x00", b"\x98\x05\x04"):
            with self.assertRaises(openpgp.KeyringError):
                openpgp.primary_fingerprints(data)
        self.assertEqual(openpgp.normalise(FINGERPRINT_SPACED), FLATHUB_FINGERPRINT)


class AndroidHarness(Harness):
    def __init__(self, fingerprint: str = FINGERPRINT_SPACED, binder: bool = True):
        super().__init__()
        self.write(android.CONF, CONF.format(fp=fingerprint))
        self.write(android.KEYRING, flathub_key())
        if binder:
            self.write(f"/usr/lib/modules/{self.ctx.kernel_release}/kernel/drivers/android/binder_linux.ko.xz", b"")
        self.runner.on(("-s", "install", "waydroid"),
                       Completed(0, "Inst libglibutil (1.0.80 repo.waydro.id [amd64])\n"
                                    "Inst lxc (1:6.0.4-4 Debian:13.7/stable [amd64])\n"
                                    "Inst waydroid (1.5.4 repo.waydro.id [all])\n", ""))

    def waydroid(self) -> None:
        self.write("/usr/bin/waydroid", "#!/bin/sh\n", 0o755)


class RuntimeInstallTest(unittest.TestCase):
    def test_writes_repository_pin_and_module_files(self):
        h = AndroidHarness()
        h.runner.script([("line", "Hit:1 http://deb.debian.org/debian trixie InRelease"),
                         ("line", "Get:2 https://repo.waydro.id trixie InRelease [4,321 B]")])
        h.runner.script([("status", "dlstatus:1:50:Retrieving waydroid"), ("status", "pmstatus:waydroid:80:Unpacking")])
        self.assertEqual(run(h, "android-runtime-install"), 0, h.events()[-1])
        sources = Path(h.path(android.SOURCES)).read_text()
        self.assertIn("Types: deb\nURIs: https://repo.waydro.id\nSuites: trixie\nComponents: main\n"
                      "Signed-By: /usr/share/boswas/keyrings/waydroid.gpg\n", sources)
        pref = Path(h.path(android.PREF)).read_text()
        specific = pref.index("Package: waydroid python3-gbinder libgbinder libglibutil")
        catch_all = pref.index("Package: *")
        self.assertLess(specific, catch_all, "the specific record must come first")
        self.assertIn('Pin: origin "repo.waydro.id"\nPin-Priority: 500', pref)
        self.assertIn('Package: *\nPin: origin "repo.waydro.id"\nPin-Priority: -1', pref)
        self.assertIn("options binder_linux devices=binder,hwbinder,vndbinder\n",
                      Path(h.path(android.MODPROBE)).read_text())
        self.assertEqual([line for line in Path(h.path(android.MODULES_LOAD)).read_text().splitlines()
                          if not line.startswith("#")], ["binder_linux"])
        for path in (android.SOURCES, android.PREF, android.MODPROBE, android.MODULES_LOAD):
            self.assertEqual(os.stat(h.path(path)).st_mode & 0o777, 0o644)
        runs = h.runner.commands("run")
        self.assertIn(["/usr/sbin/modprobe", "binder_linux"], runs)
        self.assertIn(["/usr/bin/systemctl", "disable", "--now", "waydroid-container.service"], runs)
        self.assertEqual(h.runner.commands("stream")[1][-3:], ["-y", "install", "waydroid"])
        for command in h.runner.commands():
            self.assertNotIn("GAPPS", command)
        h.close()

    def test_fingerprint_mismatch_is_untrusted_and_changes_nothing(self):
        h = AndroidHarness(fingerprint="0" * 40)
        self.assertEqual(run(h, "android-runtime-install"), 2)
        result = h.events()[-1]
        self.assertEqual(result["code"], "UNTRUSTED")
        self.assertEqual(result["data"]["found"], [FLATHUB_FINGERPRINT])
        self.assertFalse(h.root.exists(android.SOURCES))
        self.assertEqual(h.runner.calls, [])
        h.close()

    def test_keyring_with_two_primary_keys_is_untrusted(self):
        h = AndroidHarness()
        h.write(android.KEYRING, flathub_key() + flathub_key())
        self.assertEqual(run(h, "android-runtime-install"), 2)
        self.assertEqual(h.events()[-1]["code"], "UNTRUSTED")
        h.close()

    def test_missing_keyring_or_conf(self):
        h = AndroidHarness()
        os.unlink(h.path(android.KEYRING))
        self.assertEqual(run(h, "android-runtime-install"), 2)
        self.assertEqual(h.events()[-1]["code"], "UNTRUSTED")
        h.close()
        h = Harness()
        self.assertEqual(run(h, "android-runtime-install"), 6)
        self.assertEqual(h.events()[-1]["code"], "UNSUPPORTED")
        h.close()

    def test_invalid_conf(self):
        for key, value in (("REPO_URL", "http://repo.waydro.id"), ("REPO_URL", "https://user:pw@repo.waydro.id"),
                           ("REPO_URL", "https://repo.waydro.id/?x=1"), ("REPO_SUITE", "trixie main"),
                           ("ALLOWED_PACKAGES", "waydroid *"), ("KEY_FINGERPRINT", "1234")):
            h = AndroidHarness()
            text = Path(h.path(android.CONF)).read_text()
            lines = [f'{key}="{value}"' if line.startswith(f"{key}=") else line for line in text.splitlines()]
            h.write(android.CONF, "\n".join(lines) + "\n")
            self.assertNotEqual(run(h, "android-runtime-install"), 0, f"{key}={value}")
            self.assertFalse(h.root.exists(android.SOURCES))
            h.close()

    def test_no_binder_is_unsupported(self):
        h = AndroidHarness(binder=False)
        self.assertEqual(run(h, "android-runtime-install"), 6)
        self.assertEqual(h.events()[-1]["code"], "UNSUPPORTED")
        h.close()

    def test_policy_deny(self):
        h = AndroidHarness()
        h.write("/etc/boswas/apps.conf", 'ANDROID_RUNTIME="deny"\n')
        self.assertEqual(run(h, "android-runtime-install"), 2)
        self.assertEqual(h.events()[-1]["code"], "NOT_ALLOWED")
        h.close()

    def test_failed_install_rolls_back_the_repository(self):
        h = AndroidHarness()
        h.runner.script([("line", "Get:1 https://repo.waydro.id trixie InRelease")])
        h.runner.script([("line", "E: Failed to fetch https://repo.waydro.id/pool/waydroid.deb  Connection timed out")],
                        code=100)
        self.assertEqual(run(h, "android-runtime-install"), 5)
        for path in (android.SOURCES, android.PREF, android.MODPROBE, android.MODULES_LOAD):
            self.assertFalse(h.root.exists(path), path)
        h.close()

    def test_unreachable_repository(self):
        h = AndroidHarness()
        h.runner.script([("line", "Hit:1 http://deb.debian.org/debian trixie InRelease"),
                         ("line", "Err:2 https://repo.waydro.id trixie InRelease"),
                         ("line", "  Could not resolve 'repo.waydro.id'")])
        self.assertEqual(run(h, "android-runtime-install"), 5)
        self.assertFalse(h.root.exists(android.SOURCES))
        self.assertEqual(len(h.runner.commands("stream")), 1, "waydroid is not installed")
        h.close()

    def test_cancel_during_refresh_rolls_back(self):
        h = AndroidHarness()
        h.runner.script([("status", "dlstatus:1:10:Retrieving"), ("cancel",)])
        self.assertEqual(run(h, "android-runtime-install"), 4)
        self.assertFalse(h.root.exists(android.SOURCES))
        h.close()

    def test_existing_files_survive_a_failed_reinstall(self):
        h = AndroidHarness()
        h.write(android.SOURCES, "existing\n")
        h.runner.script([("line", "Get:1 https://repo.waydro.id trixie InRelease")])
        h.runner.script([], code=100)
        run(h, "android-runtime-install")
        self.assertTrue(h.root.exists(android.SOURCES))
        h.close()


WAYDROID_FILES = ("/.\n/usr\n/usr/bin/waydroid\n/usr/share/applications\n/usr/share/applications/Waydroid.desktop\n"
                  "/usr/share/applications/waydroid.app.install.desktop\n/usr/share/applications/../evil.desktop\n"
                  "/usr/share/waydroid-extra/images\n")


class WaydroidEntriesTest(unittest.TestCase):
    def installed(self, foreign: bool = False) -> AndroidHarness:
        h = AndroidHarness()
        h.runner.on(("-L", "waydroid"), Completed(0, WAYDROID_FILES, ""))
        if foreign:
            h.write("/usr/local/share/applications/Waydroid.desktop", "[Desktop Entry]\nName=Mine\n")
        h.runner.script([("line", "Get:1 https://repo.waydro.id trixie InRelease")])
        h.runner.script([("status", "pmstatus:waydroid:80:Unpacking")])
        self.assertEqual(run(h, "android-runtime-install"), 0, h.events()[-1])
        return h

    def test_install_hides_waydroid_launcher_and_apk_handler(self):
        h = self.installed()
        for name in ("Waydroid.desktop", "waydroid.app.install.desktop"):
            text = Path(h.path(f"/usr/local/share/applications/{name}")).read_text()
            self.assertIn("Hidden=true", text)
            self.assertIn(android.OVERRIDE_MARKER, text)
            self.assertEqual(os.stat(h.path(f"/usr/local/share/applications/{name}")).st_mode & 0o777, 0o644)
        self.assertFalse(h.root.exists("/usr/local/share/evil.desktop"))
        self.assertEqual(h.events()[-1]["data"]["hidden_desktop_entries"],
                         ["/usr/local/share/applications/Waydroid.desktop",
                          "/usr/local/share/applications/waydroid.app.install.desktop"])
        self.assertEqual(Path(h.path(android.OVERRIDE_RECORD)).read_text().splitlines(),
                         h.events()[-1]["data"]["hidden_desktop_entries"])
        h.close()

    def test_a_file_boswas_did_not_write_is_left_alone(self):
        h = self.installed(foreign=True)
        self.assertEqual(Path(h.path("/usr/local/share/applications/Waydroid.desktop")).read_text(),
                         "[Desktop Entry]\nName=Mine\n")
        self.assertTrue(any("not written by Boswas" in e["message"] for e in h.kinds("log")))
        h.close()

    def test_remove_deletes_exactly_the_recorded_overrides(self):
        # The state android-runtime-install leaves (see the test above), plus a foreign entry.
        h = AndroidHarness()
        h.waydroid()
        recorded = []
        for name in ("Waydroid.desktop", "waydroid.app.install.desktop"):
            path = f"/usr/local/share/applications/{name}"
            h.write(path, android.override_text(name))
            recorded.append(path)
        # A recorded path that someone replaced with their own file is kept.
        h.write("/usr/local/share/applications/replaced.desktop", "[Desktop Entry]\nName=Replaced\n")
        h.write(android.OVERRIDE_RECORD, "\n".join(recorded + ["/usr/local/share/applications/replaced.desktop",
                                                               "/etc/passwd"]) + "\n")
        h.write("/usr/local/share/applications/other.desktop", "[Desktop Entry]\nName=Other\n")
        h.runner.on(("-W",), Completed(0, "waydroid\tinstalled\n", ""))
        h.runner.on(("-s", "remove"), Completed(0, "Remv waydroid [1.5.4]\n", ""))
        self.assertEqual(run(h, "android-runtime-remove"), 0, h.events()[-1])
        for name in ("Waydroid.desktop", "waydroid.app.install.desktop"):
            self.assertFalse(h.root.exists(f"/usr/local/share/applications/{name}"), name)
        self.assertTrue(h.root.exists("/usr/local/share/applications/other.desktop"))
        self.assertTrue(h.root.exists("/usr/local/share/applications/replaced.desktop"))
        self.assertFalse(h.root.exists(android.OVERRIDE_RECORD))
        h.close()

    def test_init_refuses_when_waydroid_is_configured_for_gapps(self):
        h = AndroidHarness()
        h.waydroid()
        h.write("/var/lib/waydroid/waydroid.cfg",
                "[waydroid]\nsystem_ota = https://ota.waydro.id/system/lineage/waydroid_x86_64/GAPPS.json\n")
        self.assertEqual(run(h, "android-init"), 2)
        self.assertEqual(h.events()[-1]["code"], "NOT_ALLOWED")
        self.assertEqual(h.runner.commands("stream"), [])
        h.close()


class RuntimeOtherTest(unittest.TestCase):
    def test_init_downloads_vanilla_only(self):
        h = AndroidHarness()
        h.waydroid()
        h.runner.script([("line", "[09:00:01] Downloading https://ota.waydro.id/system/lineage/x.zip"),
                         ("line", "[Downloading] 100.00 MB/ 800.00 MB    5000.00 kbps(approx.)"),
                         ("line", "[Downloading] 800.00 MB/ 800.00 MB    5000.00 kbps(approx.)"),
                         ("line", "[09:10:00] Validating system image"),
                         ("line", "[09:10:30] Extracting to /var/lib/waydroid/images")])
        self.assertEqual(run(h, "android-init"), 0)
        self.assertEqual(h.runner.commands("stream")[0], ["/usr/bin/waydroid", "init", "-s", "VANILLA"])
        phases = [(p["phase"], p["cancellable"]) for p in h.kinds("progress")]
        self.assertIn(("download", True), phases)
        self.assertEqual(phases[-1], ("install", False))
        percents = [p["percent"] for p in h.kinds("progress") if p["phase"] == "download" and p["percent"]]
        self.assertIn(12.5, percents)
        h.close()

    def test_init_cancel_during_download(self):
        h = AndroidHarness()
        h.waydroid()
        h.runner.script([("line", "[Downloading] 10.00 MB/ 800.00 MB"), ("cancel",)])
        self.assertEqual(run(h, "android-init"), 4)
        h.close()

    def test_init_cancel_refused_during_extraction(self):
        h = AndroidHarness()
        h.waydroid()
        h.runner.script([("line", "Extracting to /var/lib/waydroid/images"), ("cancel",), ("line", "Done")])
        self.assertEqual(run(h, "android-init"), 0)
        self.assertTrue(any(e["level"] == "warning" for e in h.kinds("log")))
        h.close()

    def test_init_refuses_gapps_configuration(self):
        h = AndroidHarness()
        h.waydroid()
        h.write(android.CONF, CONF.format(fp=FINGERPRINT_SPACED).replace('"VANILLA"', '"GAPPS"'))
        self.assertEqual(run(h, "android-init"), 2)
        self.assertEqual(h.events()[-1]["code"], "NOT_ALLOWED")
        self.assertEqual(h.runner.calls, [])
        h.close()

    def test_init_without_runtime(self):
        h = AndroidHarness()
        self.assertEqual(run(h, "android-init"), 6)
        h.close()

    def test_init_network_failure(self):
        h = AndroidHarness()
        h.waydroid()
        h.runner.script([("line", "urllib.error.URLError: <urlopen error [Errno -3] Temporary failure in name "
                                  "resolution>")], code=1)
        self.assertEqual(run(h, "android-init"), 5)
        h.close()

    def test_init_interrupted_download_fails_the_checksum_and_is_retryable(self):
        # Seen in the VM test: the SourceForge download stopped at 27 %, Waydroid refused the image.
        h = AndroidHarness()
        h.waydroid()
        h.runner.script([("line", "[Downloading] 250.00 MB/ 900.00 MB"),
                         ("line", "ERROR: Downloaded system image hash doesn't match, expected: 0535527"),
                         ("line", "See also: <https://github.com/waydroid>")], code=1)
        self.assertEqual(run(h, "android-init"), 5)
        result = h.events()[-1]
        self.assertEqual(result["code"], "NETWORK")
        self.assertIn("checksum did not match", result["message"])
        h.close()

    def test_service(self):
        h = AndroidHarness()
        h.waydroid()
        self.assertEqual(run(h, "android-service", "start"), 0)
        self.assertEqual(h.runner.commands()[-1], ["/usr/bin/systemctl", "start", "waydroid-container.service"])
        self.assertIn("started", h.events()[-1]["message"])
        h.close()
        h = AndroidHarness()
        h.waydroid()
        self.assertEqual(run(h, "android-service", "enable"), 2)
        h.close()
        h = AndroidHarness()
        self.assertEqual(run(h, "android-service", "stop"), 6)
        h.close()

    def test_remove_and_purge(self):
        h = AndroidHarness()
        h.waydroid()
        for path in (android.SOURCES, android.PREF, android.MODPROBE, android.MODULES_LOAD):
            h.write(path, "x\n")
        h.write("/var/lib/waydroid/images/system.img", b"x" * 1000)
        h.runner.on(("-W",), Completed(0, "waydroid\tinstalled\npython3-gbinder\tinstalled\nlibgbinder\tnot-installed\n",
                                       ""))
        h.runner.on(("-s", "remove"), Completed(0, "Remv waydroid [1.5.4]\nRemv python3-gbinder [1.1]\n", ""))
        self.assertEqual(run(h, "android-runtime-remove", "--purge-data"), 0)
        self.assertEqual(h.runner.commands("stream")[0][-4:], ["-y", "remove", "waydroid", "python3-gbinder"])
        for path in (android.SOURCES, android.PREF, android.MODPROBE, android.MODULES_LOAD, "/var/lib/waydroid"):
            self.assertFalse(h.root.exists(path), path)
        self.assertTrue(h.events()[-1]["data"]["purged"])
        h.close()

    def test_purge_refuses_a_symlinked_data_dir(self):
        h = AndroidHarness()
        os.makedirs(h.path("/elsewhere"))
        os.makedirs(os.path.dirname(h.path(android.DATA_DIR)), exist_ok=True)
        os.symlink(h.path("/elsewhere"), h.path(android.DATA_DIR))
        self.assertEqual(run(h, "android-runtime-remove", "--purge-data"), 1)
        self.assertTrue(os.path.isdir(h.path("/elsewhere")))
        h.close()

    def test_repair(self):
        h = AndroidHarness()
        h.waydroid()
        h.runner.script([])
        self.assertEqual(run(h, "android-repair"), 0)
        self.assertEqual(h.runner.commands("stream")[0], ["/usr/bin/waydroid", "upgrade", "--offline"])
        self.assertEqual(h.runner.commands("run")[-1], ["/usr/bin/systemctl", "start", "waydroid-container.service"])
        h.close()


class FlatpakTest(unittest.TestCase):
    def harness(self) -> Harness:
        h = Harness(cancellable=False)
        h.write(f"{flatpak.REMOTES_DIR}/flathub.flatpakrepo", FLATHUB_REPO.read_bytes())
        h.write("/usr/bin/flatpak", "#!/bin/sh\n", 0o755)
        return h

    def test_enable(self):
        h = self.harness()
        self.assertEqual(run(h, "flatpak-remote-enable", "flathub"), 0)
        self.assertEqual(h.runner.commands()[-1],
                         ["/usr/bin/flatpak", "remote-add", "--system", "--if-not-exists", "flathub",
                          "/usr/share/boswas/sysops/flatpak-remotes/flathub.flatpakrepo"])
        h.close()

    def test_not_allowed_by_policy_or_not_shipped(self):
        h = self.harness()
        h.write("/etc/boswas/apps.conf", 'FLATPAK_REMOTES=""\n')
        self.assertEqual(run(h, "flatpak-remote-enable", "flathub"), 2)
        self.assertEqual(h.events()[-1]["code"], "NOT_ALLOWED")
        h.close()
        h = self.harness()
        h.write("/etc/boswas/apps.conf", 'FLATPAK_REMOTES="flathub other"\n')
        self.assertEqual(run(h, "flatpak-remote-enable", "other"), 2)
        self.assertEqual(h.runner.calls, [])
        h.close()

    def test_writable_definition_is_untrusted(self):
        h = self.harness()
        os.chmod(h.path(f"{flatpak.REMOTES_DIR}/flathub.flatpakrepo"), 0o666)
        self.assertEqual(run(h, "flatpak-remote-enable", "flathub"), 2)
        self.assertEqual(h.events()[-1]["code"], "UNTRUSTED")
        h.close()

    def test_no_flatpak(self):
        h = self.harness()
        os.unlink(h.path("/usr/bin/flatpak"))
        self.assertEqual(run(h, "flatpak-remote-enable", "flathub"), 6)
        h.close()

    def test_disable(self):
        h = self.harness()
        self.assertEqual(run(h, "flatpak-remote-disable", "flathub"), 0)
        self.assertEqual(h.runner.commands()[-1], ["/usr/bin/flatpak", "remote-delete", "--system", "flathub"])
        h.close()
        h = self.harness()
        h.runner.on(("remote-delete",), Completed(1, "", "error: Remote \"flathub\" not found"))
        self.assertEqual(run(h, "flatpak-remote-disable", "flathub"), 0)
        h.close()

    def test_shipped_definition_carries_the_verified_key(self):
        text = FLATHUB_REPO.read_text(encoding="utf-8")
        self.assertIn("Url=https://dl.flathub.org/repo/", text)
        self.assertEqual(openpgp.primary_fingerprints(flathub_key()), [FLATHUB_FINGERPRINT])


if __name__ == "__main__":
    unittest.main()
