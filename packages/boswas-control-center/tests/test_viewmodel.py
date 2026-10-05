"""Presentation rules of Control Center (no Qt): status mapping, parsing, summaries.

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import copy
import sys
import unittest
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402

from boswas_control_center import viewmodel as vm  # noqa: E402

UTC = timezone.utc


def check(status: str, detail: str = "detail", check_id: str = "x") -> dict:
    return {"id": check_id, "title": check_id.title(), "status": status, "detail": detail, "scored": True}


class StatusMappingTest(unittest.TestCase):
    def test_cli_statuses(self):
        self.assertEqual(vm.check_state(check("PASS")), vm.SECURE)
        self.assertEqual(vm.check_state(check("WARN")), vm.WARNING)
        self.assertEqual(vm.check_state(check("FAIL")), vm.ATTENTION)
        self.assertEqual(vm.check_state(check("INFO")), vm.INFO)
        self.assertEqual(vm.check_state(check("UNKNOWN")), vm.UNAVAILABLE)
        self.assertEqual(vm.check_state(check("UNKNOWN", "check failed: boom")), vm.ERROR)
        self.assertEqual(vm.check_state(check("BOGUS")), vm.ERROR)
        self.assertEqual(vm.check_state("not a dict"), vm.ERROR)

    def test_labels_are_words(self):
        self.assertEqual(vm.STATE_LABELS[vm.SECURE], "Secure")
        self.assertEqual(vm.STATE_LABELS[vm.WARNING], "Warning")
        self.assertEqual(vm.STATE_LABELS[vm.ATTENTION], "Attention")
        self.assertEqual(vm.STATE_LABELS[vm.ERROR], "Error")
        self.assertEqual(vm.STATE_LABELS[vm.UNAVAILABLE], "Not available")

    def test_security_view_from_the_booted_vm_document(self):
        view = vm.security_view(fx.STATUS, None, fx.AGENT, None, fx.RUNTIME)
        rows = {r.key: r for r in view.rows}
        self.assertEqual([r.key for r in view.rows], list(vm.SECURITY_ROW_KEYS))
        self.assertEqual(rows["apparmor"].state, vm.WARNING)
        self.assertEqual(rows["firewall"].state, vm.SECURE)
        self.assertEqual(rows["firewall"].detail, "nftables.service active (Boswas baseline ruleset)")
        self.assertEqual(rows["secure-boot"].state, vm.WARNING)
        self.assertEqual(rows["secure-boot"].detail, "Legacy BIOS boot; Boswas OS is UEFI-first")
        self.assertEqual(rows["disk-encryption"].state, vm.INFO)
        self.assertEqual(rows["screen-lock"].state, vm.SECURE)
        self.assertEqual(rows["updates"].state, vm.SECURE)
        # winapp-confinement is UNKNOWN for a user; the device agent's runtime check decides.
        self.assertEqual(rows["winapp"].state, vm.SECURE)
        self.assertIn("enforcing", rows["winapp"].detail)
        self.assertEqual(rows["agent"].state, vm.SECURE)
        self.assertEqual(rows["agent"].detail, "Running: Ready · Control Plane: Standalone · Not enrolled")
        self.assertEqual(rows["policy"].state, vm.INFO)
        other = {r.key: r for r in view.other_rows}
        self.assertEqual(set(other), {"check:tpm", "check:audit", "check:ssh-server", "check:root-account",
                                      "check:apt-trust", "check:usb-policy"})
        self.assertEqual(other["check:root-account"].state, vm.UNAVAILABLE)
        self.assertEqual(other["check:root-account"].detail, "Only an administrator can check this")
        self.assertEqual(view.summary.state, vm.WARNING)
        self.assertEqual(view.summary.headline, "Protected, with 3 items to review")
        self.assertTrue(view.basis.startswith("Local self-assessment"))

    def test_never_invents_a_check(self):
        status = {"checks": [check("PASS", "ok", "firewall")]}
        view = vm.security_view(status, None, None, "down")
        rows = {r.key: r for r in view.rows}
        self.assertEqual(rows["firewall"].state, vm.SECURE)
        for key in ("apparmor", "secure-boot", "disk-encryption", "screen-lock", "updates"):
            self.assertEqual(rows[key].state, vm.UNAVAILABLE, key)
            self.assertIn("not reported", rows[key].detail)
        self.assertEqual(view.other_rows, [])

    def test_fail_needs_attention(self):
        status = copy.deepcopy(fx.STATUS)
        status["checks"][3] = check("FAIL", "nftables.service is inactive", "firewall")
        view = vm.security_view(status, None, fx.AGENT, None, fx.RUNTIME)
        self.assertEqual(next(r for r in view.rows if r.key == "firewall").state, vm.ATTENTION)
        self.assertEqual(view.summary.state, vm.ATTENTION)
        self.assertEqual(view.summary.headline, "1 item needs your attention")

    def test_status_error_marks_every_cli_row_as_error(self):
        view = vm.security_view(None, "boswas is not installed on this system.", fx.AGENT, None)
        for row in view.rows[:7]:
            self.assertEqual(row.state, vm.ERROR, row.key)
            self.assertIn("could not be read", row.detail)
        self.assertEqual(view.summary.state, vm.ERROR)

    def test_loading(self):
        view = vm.security_view(None, None, None, None, loading=True, agent_loading=True)
        self.assertTrue(all(r.state == vm.CHECKING for r in view.rows))
        self.assertEqual(view.summary.state, vm.CHECKING)

    def test_device_agent_states(self):
        self.assertEqual(vm.agent_row(None, "not running").state, vm.ATTENTION)
        self.assertEqual(vm.agent_row({**fx.AGENT, "state": "ERROR"}, None).state, vm.ERROR)
        self.assertEqual(vm.agent_row({**fx.AGENT, "state": "DEGRADED", "reasons": ["clock"]}, None).state,
                         vm.WARNING)
        self.assertEqual(vm.agent_row({**fx.AGENT, "connection": "REVOKED"}, None).state, vm.ATTENTION)
        self.assertEqual(vm.agent_row({"available": False}, "x").state, vm.ATTENTION)

    def test_policy_row(self):
        managed = vm.policy_row(fx.AGENT_ENROLLED, None)
        self.assertEqual(managed.state, vm.SECURE)
        self.assertIn("2026.10.1", managed.detail)
        self.assertEqual(vm.policy_row(None, "down").state, vm.UNAVAILABLE)

    def test_winapp_row(self):
        runtime = copy.deepcopy(fx.RUNTIME)
        self.assertEqual(vm.winapp_row(check("PASS", "x", "winapp-confinement"), None).state, vm.SECURE)
        self.assertEqual(vm.winapp_row(check("INFO", "WinCompat (boswas-compat) not installed"), None).state,
                         vm.INFO)
        runtime["apparmor"]["mode"] = "complain"
        self.assertEqual(vm.winapp_row(check("UNKNOWN", "run as root to check"), runtime).state, vm.WARNING)
        runtime["apparmor"]["mode"] = "not-loaded"
        self.assertEqual(vm.winapp_row(None, runtime).state, vm.WARNING)
        runtime["apparmor"]["mode"] = "enforce"
        runtime["bubblewrap"]["available"] = False
        self.assertEqual(vm.winapp_row(None, runtime).state, vm.ATTENTION)
        self.assertEqual(vm.winapp_row(check("UNKNOWN", "run as root to check"), None).state, vm.UNAVAILABLE)

    def test_security_line(self):
        view = vm.security_view(fx.STATUS, None, fx.AGENT, None, fx.RUNTIME)
        self.assertEqual(vm.security_line(view), "7 protections active, 3 items to review")
        self.assertEqual(vm.security_line(None), "Not checked yet")


class ReleaseTest(unittest.TestCase):
    def test_parse_env_without_evaluation(self):
        text = ('# comment\nBOSWAS_NAME="Boswas OS"\nBOSWAS_VERSION=\'v1 Alpha\'\nEVIL="$(rm -rf /)"\n'
                'BACKTICK=`id`\nnot a line\n1BAD="x"\nBROKEN="unclosed\nSPACED = "ok"\n')
        env = vm.parse_env(text)
        self.assertEqual(env["BOSWAS_NAME"], "Boswas OS")
        self.assertEqual(env["BOSWAS_VERSION"], "v1 Alpha")
        self.assertEqual(env["EVIL"], "$(rm -rf /)")          # data, never run
        self.assertEqual(env["BACKTICK"], "`id`")
        self.assertNotIn("1BAD", env)
        self.assertNotIn("BROKEN", env)
        self.assertEqual(vm.parse_env(None), {})

    def test_release_info(self):
        info = vm.release_info({"release": vm.parse_env(fx.RELEASE), "image": vm.parse_env(fx.IMAGE_INFO),
                                "update": {"CHANNEL": "qa"}, "debian_version": "13.7"})
        self.assertEqual(info.name, "Boswas OS")
        self.assertEqual(info.version_text, "v1 Alpha (1.0~alpha3)")
        self.assertEqual(info.build_id, "BOS-1.0~alpha3-20261004T144858Z")
        self.assertEqual(info.channel, "qa")                 # update.conf wins over the release file
        self.assertEqual(info.git_commit, "205e3f45b5fe")
        self.assertEqual(info.debian_version, "13.7")
        empty = vm.release_info(None)
        self.assertEqual(empty.name, "Boswas OS")
        self.assertEqual(empty.version_text, "Unknown")

    def test_technical_rows(self):
        info = vm.release_info({"release": vm.parse_env(fx.RELEASE), "debian_version": "13.7"})
        rows = dict(vm.technical_rows(info, {"boot_mode": "UEFI", "hostname": "pc"}))
        self.assertEqual(rows["Package base"], "Debian 13.7")
        self.assertEqual(rows["Boot mode"], "UEFI")
        self.assertNotIn("Kernel command line", rows)

    def test_about_rows(self):
        info = vm.release_info({"release": vm.parse_env(fx.RELEASE), "image": vm.parse_env(fx.IMAGE_INFO)})
        hardware = {"cpu_model": "CPU X", "cpu_threads": 8, "memory_bytes": 16 * 1024 ** 3, "kernel": "6.12",
                    "machine": "x86_64"}
        storage = [{"key": "system", "title": "System", "path": "/", "total": 100 * 1024 ** 3,
                    "free": 40 * 1024 ** 3}]
        rows = dict(vm.about_rows(info, hardware, storage, "6.3.6", True, None, None, fx.AGENT, None))
        self.assertEqual(rows["Version"], "v1 Alpha (1.0~alpha3)")
        self.assertEqual(rows["Processor"], "CPU X (8 threads)")
        self.assertEqual(rows["Memory"], "16.0 GiB")
        self.assertEqual(rows["Storage"], "100.0 GiB (40.0 GiB free)")
        self.assertEqual(rows["Desktop"], "KDE Plasma 6.3.6")
        self.assertEqual(rows["Session"], "Live session")
        self.assertEqual(rows["Device agent"], "Running (Ready), Control Plane: Standalone")
        rows = dict(vm.about_rows(info, {}, [], None, False, None, None, None, "down"))
        self.assertEqual(rows["Desktop"], "Not available")
        self.assertEqual(rows["Session"], "Installed")
        self.assertEqual(rows["Device agent"], "Not running")


class LiveSessionTest(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(vm.is_live(True, ""))
        self.assertTrue(vm.is_live(False, "BOOT_IMAGE=/live/vmlinuz boot=live components"))
        self.assertTrue(vm.is_live(False, "", "live\n"))
        self.assertFalse(vm.is_live(False, "root=UUID=1 ro quiet"))
        self.assertFalse(vm.is_live(False, "root=/dev/sda1 noboot=live"))
        self.assertFalse(vm.is_live(False, "", "installed\n"))
        self.assertFalse(vm.is_live(False, None, None))


class HardwareTest(unittest.TestCase):
    def test_cpuinfo_and_meminfo(self):
        self.assertEqual(vm.parse_cpuinfo(fx.CPUINFO), ("Intel(R) Core(TM) i7-1265U", 2))
        self.assertEqual(vm.parse_meminfo(fx.MEMINFO), 16303520 * 1024)
        self.assertEqual(vm.parse_cpuinfo(None), ("", 0))
        self.assertIsNone(vm.parse_meminfo("nothing"))

    def test_plasma_version(self):
        self.assertEqual(vm.plasma_version_from_metainfo(fx.METAINFO), "6.3.6")
        self.assertEqual(vm.plasma_version_from_output("plasmashell 6.3.6\n"), "6.3.6")
        self.assertIsNone(vm.plasma_version_from_output("crash"))

    def test_power(self):
        view = vm.power_view([{"name": "BAT0", "type": "Battery", "status": "Discharging", "capacity": "12"},
                              {"name": "AC", "type": "Mains", "online": "0"},
                              {"name": "hid-mouse", "type": "Battery", "scope": "Device", "capacity": "50"}])
        self.assertEqual(len(view.batteries), 1)
        self.assertEqual(view.batteries[0].percent, 12)
        self.assertEqual(view.batteries[0].state, vm.WARNING)
        self.assertEqual(view.summary, "Battery at 12%, on battery")
        self.assertFalse(view.on_mains)
        desktop = vm.power_view([{"name": "AC", "type": "Mains", "online": "1"}])
        self.assertEqual(desktop.summary, "No battery detected. This device runs on mains power.")
        self.assertIn("No battery or power adapter", vm.power_view([]).summary)

    def test_storage(self):
        gib = 1024 ** 3
        rows = vm.storage_rows([{"key": "system", "title": "System", "path": "/", "total": 100 * gib, "free": 1 * gib},
                                {"key": "home", "title": "Home", "path": "/home", "total": 0, "free": 0}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, vm.WARNING)
        self.assertEqual(rows[0].percent_used, 99)
        self.assertEqual(rows[0].text, "99.0 GiB used of 100.0 GiB (1.0 GiB free)")
        self.assertEqual(vm.format_bytes(512), "512 bytes")
        self.assertEqual(vm.format_bytes(None), "Unknown")


class UpdatesTest(unittest.TestCase):
    def test_apt_shell(self):
        self.assertEqual(vm.parse_apt_shell(fx.APT_SHELL), {"Unattended-Upgrade": "1", "Update-Package-Lists": "1",
                                                            "Download-Upgradeable-Packages": "",
                                                            "AutocleanInterval": "7"})
        self.assertEqual(vm.parse_apt_shell("UU='1'; rm -rf /\nXX='2'\n"), {})

    def test_apt_conf_later_file_wins(self):
        values = vm.parse_apt_conf(['APT::Periodic::Unattended-Upgrade "1";', 'APT::Periodic::Unattended-Upgrade "0";'])
        self.assertEqual(values, {"Unattended-Upgrade": "0"})

    def test_view(self):
        doc = {"periodic": {"Unattended-Upgrade": "1", "Update-Package-Lists": "1"}, "source": "apt-config",
               "stamps": {"update-success-stamp": fx.STAMP_TIME}, "channel": "dev", "repository": "",
               "log": {"readable": True, "lines": fx.UU_LOG.splitlines()}}
        view = vm.updates_view(doc, "security-only", tz=UTC)
        rows = dict(view.rows)
        self.assertEqual(view.summary.state, vm.SECURE)
        self.assertIn("no manual updater", view.summary.detail)
        self.assertEqual(rows["Package lists refreshed"], "Every day")
        self.assertEqual(rows["Last refresh"], "2026-10-04 07:46")
        self.assertEqual(rows["Last automatic update run"], "Not yet on this device")
        self.assertEqual(rows["Device update policy"], "Security updates only")
        self.assertEqual(len(view.log_lines), 2)
        off = vm.updates_view({**doc, "periodic": {"Unattended-Upgrade": "0"}, "log": {"readable": False}})
        self.assertEqual(off.summary.state, vm.WARNING)
        self.assertIn("administrators only", off.log_note)
        unknown = vm.updates_view({}, live=True)
        self.assertEqual(unknown.summary.state, vm.UNAVAILABLE)
        self.assertIn("live session", unknown.log_note)


class PrivacyTest(unittest.TestCase):
    def test_unenrolled_sends_nothing(self):
        view = vm.privacy_view(fx.AGENT, fx.CONFIG)
        rows = dict(view.rows)
        self.assertEqual(view.summary.headline, "Nothing leaves this device")
        self.assertEqual(rows["Device management"], "Not enrolled: nothing is sent")
        self.assertTrue(rows["Inventory"].startswith("Standard"))
        self.assertTrue(rows["Security status reports"].startswith("Security"))

    def test_enrolled(self):
        view = vm.privacy_view(fx.AGENT_ENROLLED, fx.CONFIG)
        self.assertEqual(view.summary.state, vm.INFO)
        self.assertIn("control.example.invalid", view.summary.detail)

    def test_only_allowlisted_settings(self):
        settings = vm.device_settings({"settings": {"CONTROL_PLANE_URL": "https://x", "INVENTORY_POLICY": "off"}})
        self.assertEqual(settings, {"INVENTORY_POLICY": "off"})

    def test_agent_down(self):
        view = vm.privacy_view(None, {"settings": {"TELEMETRY_POLICY": "none"}}, "down")
        self.assertEqual(view.summary.state, vm.UNAVAILABLE)
        self.assertTrue(dict(view.rows)["Security status reports"].startswith("None"))


class WindowsTest(unittest.TestCase):
    def test_healthy(self):
        view = vm.windows_view(fx.SYSTEM, None, fx.WINDOWS_APPS, None)
        rows = dict(view.rows)
        self.assertEqual(view.summary.state, vm.SECURE)
        self.assertEqual(rows["Supported applications"], "x86_64 / 64-bit Windows applications only")
        self.assertEqual(rows["Installed applications"], "2")
        self.assertEqual(rows["Confinement"], "Enforcing")
        self.assertEqual(vm.windows_line(view), "Runtime healthy, 2 applications installed (x86_64, 64-bit only)")

    def test_session_unavailable_falls_back_to_the_agent(self):
        view = vm.windows_view(None, "not running", None, "not running", fx.AGENT)
        self.assertEqual(view.summary.state, vm.SECURE)
        self.assertIn("session service is not running", view.summary.detail)
        view = vm.windows_view(None, "not running", None, "not running", None)
        self.assertEqual(view.summary.state, vm.UNAVAILABLE)

    def test_unhealthy(self):
        system = copy.deepcopy(fx.SYSTEM)
        system["runtime"]["healthy"] = False
        system["runtime"]["wine"]["available"] = False
        view = vm.windows_view(system, None, [], None)
        self.assertEqual(view.summary.state, vm.WARNING)
        self.assertEqual(dict(view.rows)["Wine"], "Missing")


class DesktopEntryTest(unittest.TestCase):
    def parse(self, text: str, desktop_id: str = "org.example.app", locale=None, which=None):
        return vm.parse_desktop_entry(text, desktop_id, f"/x/{desktop_id}.desktop", locale, "KDE", which)

    def test_application(self):
        entry = self.parse("[Desktop Entry]\nType=Application\nName=Kate\nName[de]=Kate DE\nGenericName=Editor\n"
                           "Comment=Edit\\stext\nIcon=kate\nExec=kate %U\nKeywords=text;editor;\n"
                           "[Desktop Action new]\nName=Wrong\n", locale="de_DE.UTF-8")
        self.assertEqual(entry.name, "Kate DE")
        self.assertEqual(entry.comment, "Edit text")
        self.assertEqual(entry.keywords, ("text", "editor"))
        self.assertEqual(self.parse("[Desktop Entry]\nType=Application\nName=A\nExec=a\n").icon, "")

    def test_skipped_entries(self):
        base = "[Desktop Entry]\nType=Application\nName=A\nExec=a\n"
        self.assertIsNone(self.parse(base + "NoDisplay=true\n"))
        self.assertIsNone(self.parse(base + "Hidden=true\n"))
        self.assertIsNone(self.parse(base + "OnlyShowIn=GNOME;XFCE;\n"))
        self.assertIsNotNone(self.parse(base + "OnlyShowIn=GNOME;KDE;\n"))
        self.assertIsNone(self.parse(base + "NotShowIn=KDE;\n"))
        self.assertIsNone(self.parse(base.replace("Application", "Link")))
        self.assertIsNone(self.parse("[Desktop Entry]\nType=Application\nName=A\n"))
        self.assertIsNone(self.parse("[Desktop Entry]\nType=Application\nExec=a\n"))
        self.assertIsNone(self.parse(base + "TryExec=/missing\n", which=lambda p: None))
        self.assertIsNotNone(self.parse(base + "TryExec=/usr/bin/a\n", which=lambda p: p))
        self.assertIsNone(self.parse(base, desktop_id="-bad id"))
        self.assertIsNone(self.parse("[Other]\nType=Application\nName=A\nExec=a\n"))

    def test_untrusted_text(self):
        rlo = chr(0x202E)
        entry = self.parse(f"[Desktop Entry]\nType=Application\nName=Evil\x1b[31m{rlo}Name\nExec=x\n")
        self.assertEqual(entry.name, "EvilName")
        self.assertNotIn("\x1b", entry.name)
        self.assertNotIn(rlo, entry.name)

    def test_locale_keys(self):
        self.assertEqual(vm.locale_keys("de_DE.UTF-8@euro"), ["de_DE@euro", "de_DE", "de@euro", "de"])
        self.assertEqual(vm.locale_keys("C"), [])

    def test_groups(self):
        entries = [vm.DesktopEntry("org.kde.kate", "/a", "Kate"), vm.DesktopEntry("com.boswas.CompatibilityManager",
                                                                                   "/b", "Compatibility Manager"),
                   vm.DesktopEntry("com.boswas.ControlCenter", "/c", "Boswas Control Center"),
                   vm.DesktopEntry("boswas-winapp-com.example.notepad", "/d", "Example Notepad",
                                   winapp_id="com.example.notepad"),
                   vm.DesktopEntry("ark", "/e", "ark")]
        groups = vm.application_groups(entries, fx.WINDOWS_APPS + ["junk", {"name": "no id"}])
        self.assertEqual([i.name for i in groups[vm.BOSWAS]], ["Compatibility Manager"])
        self.assertEqual([i.name for i in groups[vm.NATIVE]], ["ark", "Kate"])
        self.assertEqual([i.windows_id for i in groups[vm.WINDOWS]], ["com.example.notepad", "com.example.paint"])
        self.assertFalse(groups[vm.WINDOWS][0].launchable)
        self.assertTrue(groups[vm.NATIVE][1].launchable)
        self.assertEqual(groups[vm.WINDOWS][1].subtitle, "Windows application · version 1.0 · Running")
        self.assertEqual([i.name for i in vm.filter_apps(groups[vm.NATIVE], "  KATE ")], ["Kate"])
        self.assertEqual(vm.filter_apps(groups[vm.NATIVE], "nothing"), [])


class PresetTest(unittest.TestCase):
    def test_contract(self):
        presets = vm.parse_presets(fx.preset_doc(Path("/usr")))
        self.assertEqual([p.id for p in presets.presets], ["boswas-dark", "boswas-light", "classic", "odd-preview"])
        self.assertEqual(presets.current, "boswas-dark")
        dark = presets.presets[0]
        self.assertTrue(dark.current)
        self.assertEqual(dark.variant_label, "Dark")
        self.assertEqual(dark.accent, "#D9B26E")
        self.assertEqual(dark.preview, "/usr/usr/share/boswas/presets/boswas-dark.png")
        odd = presets.presets[3]
        self.assertEqual((odd.variant, odd.accent, odd.preview), ("", None, None))

    def test_bad_documents(self):
        for doc in (None, [], {"presets": "x"}, {}):
            with self.assertRaises(ValueError):
                vm.parse_presets(doc)
        self.assertIsNone(vm.parse_presets({"presets": [], "current": "../x"}).current)

    def test_ids_and_previews(self):
        self.assertTrue(vm.valid_preset_id("boswas-dark"))
        for bad in ("", "-x", "../x", "A", "x y", "x;y", "a" * 65, None):
            self.assertFalse(vm.valid_preset_id(bad), bad)
        self.assertIsNone(vm.preview_path("relative.png"))
        self.assertIsNone(vm.preview_path("/a/../b.png"))
        self.assertIsNone(vm.preview_path("/a/b.exe"))
        self.assertEqual(vm.preview_path("/a/b.SVG"), "/a/b.SVG")


class TextTest(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(vm.one_line("a\x1b[31mb\x07c\n d", 50), "abc d")
        self.assertEqual(vm.one_line("x" * 30, 10), "xxxxxxxxx…")
        self.assertEqual(vm.sentence("legacy BIOS"), "Legacy BIOS")
        self.assertEqual(vm.sentence("nftables.service active"), "nftables.service active")
        self.assertEqual(vm.sentence("mode=allow"), "mode=allow")
        self.assertEqual(vm.sentence("see /home/alice/x"), "See ~/x")


if __name__ == "__main__":
    unittest.main()
