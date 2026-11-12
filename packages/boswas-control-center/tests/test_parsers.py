"""Parsers and rules of the functional pages (pure Python; userfiles on a temporary home).

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import copy
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402

from boswas_control_center import android, filetypes, hardware, overview, software, sysinfo, userfiles  # noqa: E402
from boswas_control_center import viewmodel as vm  # noqa: E402


class SoftwareTest(unittest.TestCase):
    def test_simulated_upgrade(self):
        plan = software.parse_simulated_upgrade(fx.UPGRADE_SIM)
        self.assertEqual([u.name for u in plan.upgrades], ["libssl3t64", "openssl", "firefox-esr", "libnew1"])
        self.assertEqual(plan.security_count, 2)
        first = plan.upgrades[0]
        self.assertEqual((first.old, first.new, first.arch, first.security), ("3.5.1-1", "3.5.2-1~deb13u1", "amd64",
                                                                               True))
        self.assertTrue(plan.upgrades[3].new_install)
        self.assertEqual((plan.upgraded, plan.newly_installed, plan.to_remove, plan.held_back), (3, 1, 0, 0))
        empty = software.parse_simulated_upgrade("0 upgraded, 0 newly installed, 0 to remove and 2 not upgraded.\n")
        self.assertEqual((empty.upgrades, empty.held_back), ((), 2))
        self.assertEqual(software.parse_simulated_upgrade(None).upgrades, ())
        evil = software.parse_simulated_upgrade("Inst ../../x [1] (2 Debian:13/stable [amd64])\n")
        self.assertEqual(evil.upgrades, ())

    def test_security_origin(self):
        self.assertTrue(software.is_security_origin("Debian-Security:13/stable-security"))
        self.assertTrue(software.is_security_origin("Debian:13.1/stable, Debian-Security:13/stable-security"))
        self.assertFalse(software.is_security_origin("Debian:13.2/stable"))

    def test_apt_cache(self):
        hits = software.parse_apt_search(fx.APT_SEARCH)
        self.assertEqual([h[0] for h in hits], ["vlc", "vlc-bin"])
        policy = software.parse_apt_policy(fx.APT_POLICY)
        self.assertEqual(policy["vlc"]["installed"], "")
        self.assertEqual(policy["vlc"]["candidate"], "3.0.21-10")
        self.assertEqual(policy["vlc-bin"]["installed"], "3.0.21-10")
        merged = software.hits_with_policy(hits, policy)
        self.assertFalse(merged[0].is_installed)
        self.assertTrue(merged[1].is_installed)
        self.assertEqual(merged[0].source, "Debian (signed repository)")
        self.assertEqual(software.source_label(["100 /var/lib/dpkg/status"]),
                         "Installed locally (no repository offers it)")
        self.assertEqual(software.source_label([]), "Unknown source")
        show = software.parse_apt_show(fx.APT_SHOW)
        self.assertEqual(show["Version"], "3.0.21-10")
        self.assertEqual(show["Summary"], "multimedia player and streamer")
        self.assertIn("MPEG-2", show["Description"])
        self.assertNotIn("older stanza", show["Description"])
        self.assertEqual(software.size_text(show), "download 63.0 KiB, installed 217.0 KiB")

    def test_search_terms(self):
        for good in ("vlc", "libreoffice writer", "g++", "python3.13"):
            self.assertTrue(software.valid_search(good), good)
        for bad in ("", "-o Debug", "vlc;rm", "a" * 70, ".*", "$(id)", None):
            self.assertFalse(software.valid_search(bad), bad)

    def test_flatpak(self):
        remotes = software.parse_flatpak_remotes(fx.FLATPAK_SYSTEM_REMOTES + "bad name\tx\t\nold\thttp://x\tdisabled\n",
                                                 "system")
        self.assertEqual([(r.name, r.disabled) for r in remotes], [("flathub", False), ("old", True)])
        apps = software.parse_flatpak_list(fx.FLATPAK_USER_APPS + "not-an-id\tx\t1\tflathub\n")
        self.assertEqual([a.app_id for a in apps], ["org.gimp.GIMP"])
        found = software.parse_flatpak_search(fx.FLATPAK_SEARCH)
        self.assertEqual((found[0].app_id, found[0].remote), ("org.videolan.VLC", "flathub"))
        state = software.FlatpakState(tuple(remotes), tuple(apps), ("flathub",))
        self.assertEqual([r.name for r in state.enabled()], ["flathub"])
        self.assertEqual(state.enabled("user"), [])


class AndroidTest(unittest.TestCase):
    def test_ready(self):
        view = android.android_view(fx.ANDROID_STATUS)
        rows = dict(view.rows)
        self.assertEqual(view.summary.headline, "Android is ready")
        self.assertEqual(rows["Runtime"], "Waydroid 1.5.4")
        self.assertIn("lineage-20.0", rows["Android system"])
        self.assertEqual(rows["Container service"], "Running")
        self.assertEqual(rows["Android session"], "Stopped")
        self.assertEqual(rows["Graphics"], "Software rendering (slower) (virtio_gpu)")
        self.assertIn("system images 1.1 GiB", rows["Storage used"])
        self.assertEqual([c.state for c in view.checks], [vm.SECURE, vm.SECURE, vm.WARNING])
        self.assertIn("certified devices", view.google_play_reason)
        self.assertEqual(view.alternatives, ("fdroid", "local-apk"))
        self.assertEqual(view.blockers, ())

    def test_states(self):
        self.assertEqual(android.android_view(fx.ANDROID_STATUS_NOT_INSTALLED).summary.headline,
                         "The Android runtime is not installed")
        unsupported = android.android_view(fx.ANDROID_STATUS_UNSUPPORTED)
        self.assertEqual(unsupported.summary.state, vm.ATTENTION)
        self.assertIn("no binder support", unsupported.blockers[0])
        running = copy.deepcopy(fx.ANDROID_STATUS)
        running["session"]["running"] = True
        self.assertEqual(android.android_view(running).summary.state, vm.SECURE)
        uninit = copy.deepcopy(fx.ANDROID_STATUS)
        uninit["initialized"] = False
        self.assertEqual(android.android_view(uninit).summary.state, vm.WARNING)
        missing = android.android_view(None, "not installed", unavailable=True)
        self.assertEqual(missing.summary.detail, android.NOT_INSTALLED_MANAGER)
        self.assertIsNone(missing.installed)
        self.assertEqual(android.android_view(None, loading=True).summary.state, vm.CHECKING)
        self.assertEqual(android.android_view(None, "boom").summary.state, vm.ERROR)

    def test_google_play_is_never_offered_by_the_window(self):
        doc = copy.deepcopy(fx.ANDROID_STATUS)
        doc["google_play"] = {"available": True, "reason": "yes", "alternatives": ["google-play", "fdroid"]}
        view = android.android_view(doc)
        self.assertIn("does not offer it", view.google_play_reason)
        self.assertEqual(view.alternatives, ("fdroid",))
        doc["google_play"] = {}
        self.assertIn("did not report", android.android_view(doc).google_play_reason)

    def test_apps_and_apk(self):
        self.assertEqual([a.package for a in android.android_apps(fx.ANDROID_APPS)], ["org.fdroid.fdroid"])
        view = android.apk_view(fx.APK_DOC)
        rows = dict(view.rows)
        self.assertTrue(view.compatible)
        self.assertEqual(rows["Version"], "2.1 (21)")
        self.assertEqual(rows["Native code"], "x86_64, arm64-v8a")
        self.assertEqual(rows["Signer certificate (SHA-256)"], "cd" * 32)
        text = android.apk_confirmation(view)
        self.assertIn(android.INSPECTION_NOTE, text)
        arm = android.apk_view(fx.APK_ARM_ONLY)
        self.assertFalse(arm.compatible)
        self.assertIn("ARM translation layer", arm.problems[0])
        split = copy.deepcopy(fx.APK_DOC)
        split["apk"]["split"] = {"is_split": True, "name": "config.xxhdpi", "requires_splits": False}
        split["apk"]["compatible"] = False
        view = android.apk_view(split)
        self.assertFalse(view.compatible)
        self.assertIn("config.xxhdpi", dict(view.rows)["Split app"])
        self.assertTrue(view.problems)
        unsigned = copy.deepcopy(fx.APK_DOC)
        unsigned["apk"]["signing"] = {"schemes": [], "certificates": [{"sha256": "not-hex"}]}
        rows = dict(android.apk_view(unsigned).rows)
        self.assertEqual((rows["Signature"], rows["Signer certificate (SHA-256)"]), ("Not signed", "None found"))


class HardwareTest(unittest.TestCase):
    def test_ids_and_rows(self):
        raw = [{"address": "0000:03:00.0", "vendor": "0x10ec", "device": "0x8125", "class": "0x020000", "driver": ""},
               {"address": "0000:00:02.0", "vendor": "0x8086", "device": "0x46a6", "class": "0x030000",
                "driver": "i915"},
               {"address": "bad", "vendor": "zz", "device": "1", "class": "x"}]
        text = (Path(__file__).parent / "cc_fixtures.py").read_text(encoding="utf-8")
        self.assertIn("RTL8125", text)
        ids = ("10ec  Realtek Semiconductor Co., Ltd.\n\t8125  RTL8125 2.5GbE Controller\n\t\t10ec 8125  sub\n"
               "8086  Intel Corporation\n\t46a6  Alder Lake-P GT2\nC 02  Network controller\n\t00  Ethernet\n")
        names = hardware.parse_ids(ids, hardware.wanted_ids(raw))
        self.assertEqual(names[1][("10ec", "8125")], "RTL8125 2.5GbE Controller")
        rows = hardware.pci_rows(raw, names)
        self.assertEqual([d.category for d in rows], ["Graphics", "Network"])
        realtek = next(d for d in rows if d.vendor_id == "10ec")
        self.assertEqual((realtek.driver_text, realtek.highlighted), ("No driver in use", True))
        summary = hardware.hardware_summary(rows, [])
        self.assertEqual(summary.summary.state, vm.WARNING)
        self.assertIn("RTL8125", summary.summary.detail)
        unnamed = hardware.pci_rows(raw)
        self.assertEqual(next(d for d in unnamed if d.vendor_id == "10ec").name, "Network device 10ec:8125")

    def test_usb(self):
        raw = [{"address": "1-2", "vendor": "046d", "product_id": "c31c", "class": "00", "product": "USB Keyboard",
                "manufacturer": "Logitech", "drivers": ["usbhid"]},
               {"address": "usb1-hub", "vendor": "1d6b", "product_id": "0002", "class": "09"}]
        rows = hardware.usb_rows(raw)
        self.assertEqual([(d.name, d.category, d.driver) for d in rows], [("USB Keyboard", "Input", "usbhid")])

    def test_firmware_report(self):
        data = {"source": "journal", "missing": [
            {"file": "rtl_nic/rtl8125b-2.fw", "driver": "r8169", "device": "0000:03:00.0",
             "package": "firmware-realtek", "installed": False, "candidate": "20250410-2"},
            {"file": "rtl_nic/rtl8125b-2.fw", "driver": "dup"},
            {"file": "../../etc/shadow", "package": "x"},
            {"file": "vendor/blob.bin", "driver": "foo", "package": None, "installed": False, "candidate": None},
            {"file": "iwlwifi-x.ucode", "driver": "iwlwifi", "package": "firmware-iwlwifi", "installed": True,
             "candidate": "1"}]}
        source, missing = hardware.firmware_report(data)
        self.assertEqual(source, "journal")
        self.assertEqual([m.file for m in missing], ["rtl_nic/rtl8125b-2.fw", "vendor/blob.bin", "iwlwifi-x.ucode"])
        self.assertTrue(missing[0].installable)
        self.assertIn("firmware-realtek (20250410-2)", missing[0].explanation)
        self.assertFalse(missing[1].installable)
        self.assertIn("No Debian package provides", missing[1].explanation)
        self.assertIn("newer firmware", missing[2].explanation)
        self.assertEqual(hardware.firmware_summary(source, missing).state, vm.WARNING)
        self.assertEqual(hardware.firmware_summary("journal", []).state, vm.SECURE)
        self.assertEqual(hardware.firmware_summary(*hardware.firmware_report({})).state, vm.UNAVAILABLE)


class SysinfoTest(unittest.TestCase):
    def test_lsblk(self):
        disks = sysinfo.parse_lsblk(fx.LSBLK)
        self.assertEqual([d.name for d in disks], ["nvme0n1", "sda"])
        nvme = disks[0]
        self.assertEqual(nvme.title, "Example NVMe SSD · NVMe")
        self.assertEqual([(v.name, v.depth) for v in nvme.volumes],
                         [("nvme0n1p1", 1), ("nvme0n1p3", 1), ("nvme0n1p3_crypt", 2), ("boswas--vg-root", 3)])
        self.assertIn("mounted at /", sysinfo.volume_text(nvme.volumes[-1]))
        self.assertIn("Encrypted volume", sysinfo.volume_text(nvme.volumes[2]))
        self.assertIn("removable", disks[1].title)
        for bad in ("not json", "{}", "[]"):
            with self.assertRaises(ValueError):
                sysinfo.parse_lsblk(bad)

    def test_nmcli(self):
        self.assertEqual(sysinfo.split_terse("wlp2s0:wifi:connected:Home\\:Net"),
                         ["wlp2s0", "wifi", "connected", "Home:Net"])
        devices = sysinfo.parse_nmcli_devices(fx.NMCLI_DEVICES)
        self.assertEqual([d.device for d in devices], ["enp3s0", "wlp2s0"])      # loopback and P2P hidden
        self.assertTrue(devices[0].unavailable)
        self.assertEqual(devices[1].connection, "Home:Net")
        self.assertEqual(devices[1].kind_label, "Wi-Fi")
        addresses, gateway = sysinfo.parse_nmcli_show(fx.NMCLI_SHOW)
        self.assertEqual((addresses, gateway), (("192.168.1.23/24",), "192.168.1.1"))
        self.assertEqual([sysinfo.parse_radio(t) for t in ("enabled\n", "disabled", "?")], [True, False, None])
        state = sysinfo.NetworkState(tuple(devices), True)
        self.assertEqual(state.summary_line, "Connected (Wi-Fi: Home:Net)")
        self.assertEqual(sysinfo.network_summary(state, None, False).state, vm.SECURE)
        offline = sysinfo.NetworkState((devices[0],), True)
        self.assertEqual(sysinfo.network_summary(offline, None, False).state, vm.WARNING)
        self.assertEqual(sysinfo.network_summary(None, None, True).state, vm.UNAVAILABLE)
        for bad in ("", "a b", "x/y", "-f", "a" * 70):
            self.assertFalse(sysinfo.valid_device(bad), bad)

    def test_load(self):
        first = sysinfo.parse_proc_stat("cpu  100 0 100 800 0 0 0 0\n")
        second = sysinfo.parse_proc_stat("cpu  150 0 150 900 0 0 0 0\n")
        self.assertEqual(sysinfo.cpu_percent(first, second), 50)
        self.assertIsNone(sysinfo.cpu_percent(first, first))
        memory = sysinfo.parse_memory("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
        self.assertEqual(memory, (1024000, 256000))
        rows = dict(sysinfo.load_rows({"cpu_percent": 7, "memory": memory}))
        self.assertEqual(rows["Processor load"], "7%")
        self.assertIn("(75%)", rows["Memory in use"])
        self.assertEqual(dict(sysinfo.load_rows(None))["Memory in use"], "Unknown")


class FileTypesTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(filetypes.classify(b"!<arch>\ndebian-binary   1234"), filetypes.DEB)
        self.assertEqual(filetypes.classify(b"!<arch>\nother           "), filetypes.UNKNOWN)
        self.assertEqual(filetypes.classify(b"MZ\x90\x00"), filetypes.WINDOWS)
        self.assertEqual(filetypes.classify(bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 8), filetypes.MSI)
        self.assertEqual(filetypes.classify(b"\x7fELF\x02\x01\x01\x00AI\x02"), filetypes.APPIMAGE)
        self.assertEqual(filetypes.classify(b"\x7fELF\x02\x01\x01\x00AI\x01"), filetypes.APPIMAGE_TYPE1)
        self.assertEqual(filetypes.classify(b"\x7fELF\x02\x01\x01\x00\x00\x00\x00"), filetypes.UNKNOWN)
        self.assertEqual(filetypes.classify(b"PK\x03\x04", ["AndroidManifest.xml", "classes.dex"]), filetypes.APK)
        self.assertEqual(filetypes.classify(b"PK\x03\x04", ["readme.txt"]), filetypes.ZIP)
        self.assertEqual(filetypes.classify(b"#!/bin/sh\n"), filetypes.UNKNOWN)

    def test_detect_reads_content_not_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "innocent.deb"
            with zipfile.ZipFile(apk, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"\x03\x00")
            self.assertEqual(filetypes.detect(str(apk)).kind, filetypes.APK)
            fake = Path(tmp) / "setup.apk"
            fake.write_bytes(b"MZ" + b"\0" * 100)
            self.assertEqual(filetypes.detect(str(fake)).kind, filetypes.WINDOWS)
            with self.assertRaises(ValueError):
                filetypes.detect("relative.deb")
            with self.assertRaises(ValueError):
                filetypes.detect(tmp)                                   # a directory
            with self.assertRaises(ValueError):
                filetypes.detect(str(Path(tmp) / "missing"))


class UserFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env = {"HOME": str(self.home)}
        self.source = Path(self.tmp.name) / "Krita-5.2.6-x86_64.AppImage"
        self.source.write_bytes(b"\x7fELF\x02\x01\x01\x00AI\x02" + b"\0" * 64)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_list_remove(self):
        app = userfiles.add_appimage(self.env, str(self.source))
        self.assertEqual(app.ident, "krita-5-2-6-x86-64")
        target = self.home / "Applications" / "krita-5-2-6-x86-64.AppImage"
        self.assertEqual(Path(app.path), target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        entry = (self.home / ".local/share/applications/boswas-appimage-krita-5-2-6-x86-64.desktop").read_text()
        self.assertIn(f'Exec="{target}" %U', entry)
        self.assertIn("runs without a sandbox", entry)
        self.assertEqual([a.ident for a in userfiles.list_appimages(self.env)], [app.ident])
        with self.assertRaises(userfiles.UserFileError):
            other = Path(self.tmp.name) / "sub"
            other.mkdir()
            copy_ = other / "Krita-5.2.6-x86_64.AppImage"
            copy_.write_bytes(b"different")
            userfiles.add_appimage(self.env, str(copy_))               # would overwrite another file
        userfiles.remove_appimage(self.env, app.ident)
        self.assertFalse(target.exists())
        self.assertEqual(userfiles.list_appimages(self.env), [])
        for bad in ("../x", "", "A", "a/b"):
            with self.assertRaises(userfiles.UserFileError):
                userfiles.remove_appimage(self.env, bad)

    def test_exec_quoting(self):
        self.assertEqual(userfiles._exec_quote('/home/a b/x"y$z.AppImage'),
                         '"/home/a b/x\\\\"y\\\\$z.AppImage"')

    def test_refuses_symlinked_directories(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        os.symlink(outside, self.home / "Applications")
        with self.assertRaises(userfiles.UserFileError):
            userfiles.add_appimage(self.env, str(self.source))
        self.assertEqual(list(outside.iterdir()), [])

    def test_thumbnails_only_regular_files_below_the_cache(self):
        cache = self.home / ".cache/thumbnails/large"
        cache.mkdir(parents=True)
        (cache / "a.png").write_bytes(b"x" * 100)
        (cache / "b.png").write_bytes(b"x" * 50)
        precious = Path(self.tmp.name) / "precious.txt"
        precious.write_text("keep")
        os.symlink(precious, cache / "link.png")
        self.assertEqual(userfiles.thumbnail_usage(self.env), (2, 150))
        self.assertEqual(userfiles.clean_thumbnails(self.env), 150)
        self.assertEqual(precious.read_text(), "keep")
        self.assertEqual(userfiles.thumbnail_usage(self.env), (0, 0))

    def test_symlinked_thumbnail_root_is_left_alone(self):
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        (outside / "file").write_text("keep")
        (self.home / ".cache").mkdir()
        os.symlink(outside, self.home / ".cache/thumbnails")
        self.assertEqual(userfiles.clean_thumbnails(self.env), 0)
        self.assertTrue((outside / "file").exists())

    def test_home_required(self):
        with self.assertRaises(userfiles.UserFileError):
            userfiles.list_appimages({"HOME": "relative"})


class OverviewTest(unittest.TestCase):
    def test_rows(self):
        plan = software.parse_simulated_upgrade(fx.UPGRADE_SIM)
        network = sysinfo.NetworkState(tuple(sysinfo.parse_nmcli_devices(fx.NMCLI_DEVICES)), True)
        rows = dict(overview.overview_rows({"cpu_percent": 3, "memory": (100, 50)}, network, None, plan,
                                           ("journal", []), fx.ANDROID_STATUS, False))
        self.assertEqual(rows["Pending updates"], "4 (2 security)")
        self.assertEqual(rows["Firmware"], "No missing firmware reported")
        self.assertEqual(rows["Android apps"], "Android is ready")
        self.assertTrue(rows["Network"].startswith("Connected"))
        unknown = dict(overview.overview_rows(None, None, "nmcli failed", None, None, None, True))
        self.assertIn("Not checked", unknown["Pending updates"])
        self.assertIn("Not checked", unknown["Firmware"])
        self.assertIn("not installed", unknown["Android apps"])
        self.assertEqual(unknown["Network"], "Unknown")


if __name__ == "__main__":
    unittest.main()
