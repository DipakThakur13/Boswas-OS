"""The categories and pages, deep links, search, and the readers of the pages added with them (no Qt).

catalog: every place has a category, keys are unique, the keys the shell and
the desktop entries use exist, earlier section names are found where they
moved, search ranks titles first. The pure modules of the new pages: the
device name (device), storage cleanup (cleanup, and its writers in
userfiles), the troubleshooters (troubleshoot), cameras and printers
(peripherals), VPN connections and adapters (netconfig), fonts, installed
apps, optional features, app permissions and the update history.

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import gzip
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures as fx  # noqa: E402

from boswas_control_center import (catalog, cleanup, commands, device, features, fonts, installed, netconfig,  # noqa: E402
                                   peripherals, permissions, probes, troubleshoot, updatelog, userfiles)
from boswas_control_center import servicebridge as sb  # noqa: E402
from boswas_control_center import viewmodel as vm  # noqa: E402

# Every --page the shell, the desktop entries, the live session and the boot test use (grep boswas-desktop).
SHELL_KEYS = ("personalization", "network", "bluetooth", "display", "sound", "power", "storage", "applications",
              "windows", "android", "security", "users", "updates", "devices", "privacy", "hardware", "system",
              "about", "install", "time-language", "privacy-security", "notifications", "multitasking", "taskbar",
              "start", "text-size", "magnifier", "color-filters", "accessibility-keyboard")


class CatalogTest(unittest.TestCase):
    def test_categories_like_the_settings_of_a_modern_desktop(self):
        self.assertEqual([c.label for c in catalog.CATEGORIES],
                         ["Home", "System", "Bluetooth & devices", "Network & internet", "Personalization", "Apps",
                          "Accounts", "Time & language", "Accessibility", "Privacy & security", "Boswas Update"])
        self.assertEqual(len(set(catalog.PAGE_KEYS)), len(catalog.PAGE_KEYS))
        for page in catalog.PAGES:
            self.assertRegex(page.key, r"^[a-z][a-z-]*$")
            if not page.is_category:
                self.assertIn(page.parent, catalog.CATEGORY_KEYS, page.key)
                self.assertTrue(page.card_text, page.key)
            self.assertTrue(page.icons, page.key)
        for key in SHELL_KEYS:
            self.assertIn(key, catalog.PAGE_KEYS)
        self.assertEqual([p.key for p in catalog.children("system")],
                         ["display", "sound", "notifications", "power", "storage", "multitasking", "troubleshoot",
                          "recovery", "clipboard", "about", "install"])
        self.assertEqual([p.key for p in catalog.children("personalization")],
                         ["background", "colours", "themes", "start", "taskbar", "fonts"])
        self.assertEqual([p.key for p in catalog.children("accessibility")],
                         ["text-size", "visual-effects", "mouse-pointer", "magnifier", "color-filters",
                          "accessibility-keyboard"])
        self.assertEqual(catalog.category_of("vpn"), "network")
        self.assertEqual(catalog.category_of("network"), "network")
        self.assertEqual(catalog.path("display"), ("System", "Display"))
        self.assertEqual(catalog.path("updates"), ("Boswas Update",))
        # No product names of another vendor, and Boswas Update is not called anything else.
        text = " ".join(f"{p.label} {p.heading} {p.description} {p.summary}" for p in catalog.PAGES)
        for word in ("Windows Update", "Microsoft", "OneDrive", "Windows Hello", "Phone Link"):
            self.assertNotIn(word, text)

    def test_icons_exist_in_the_icon_theme(self):
        theme = fx.PACKAGE.parents[1] / "desktop" / "icons" / "Boswas-Prism" / "scalable"
        names = {path.stem for path in theme.rglob("*.svg")}
        for page in catalog.PAGES:
            self.assertIn(page.icons[0], names, page.key)

    def test_section_homes_cover_the_earlier_pages(self):
        self.assertEqual(catalog.section_homes("display"), ("display",))
        self.assertEqual(catalog.section_homes("system")[:3], ("system", "date-time", "region"))
        self.assertIn("display", catalog.section_homes("system"))                # its pages too
        self.assertIn("lock-screen", catalog.section_homes("security"))
        self.assertIn("keyboard", catalog.section_homes("devices"))
        self.assertEqual(catalog.SECTION_ALIASES["other-connections"], "ethernet")

    def test_search_ranks_titles_first(self):
        entries = catalog.entries([("Night light", "display", "night-light", ("blue light", "warm")),
                                   ("Wi-Fi", "wifi", "wi-fi", ("wireless",))])
        found = catalog.search("night", entries)
        self.assertEqual(found[0].title, "Night light")
        self.assertEqual(found[0].kind, "section")
        self.assertEqual([e.title for e in catalog.search("display", entries)][0], "Display")
        self.assertEqual(catalog.search("", entries), [])
        self.assertEqual(catalog.search("zebra", entries), [])
        # A section with the name of its page is the page: one result, which keeps the section's words.
        wifi = [e for e in catalog.search("wireless", entries) if e.page == "wifi"]
        self.assertEqual(len(wifi), 1)
        self.assertEqual(wifi[0].kind, "page")
        hidden = catalog.search("install", entries, visible=lambda key: key != "install")
        self.assertNotIn("install", [e.page for e in hidden])
        self.assertEqual(catalog.search("rename", entries)[0].title, "Rename this device")

    def test_weather_on_the_lock_screen_is_found(self):
        found = catalog.search("weather", catalog.entries())
        self.assertTrue(found)
        self.assertEqual((found[0].title, found[0].page, found[0].section),
                         ("Show weather on the lock screen", "lock-screen", "lock-screen"))
        self.assertIn("lock-screen", [entry.page for entry in catalog.search("forecast", catalog.entries())])

    def test_single_settings_point_at_places_that_exist(self):
        for setting in catalog.SETTINGS:
            self.assertIn(setting.page, catalog.BY_KEY, setting.title)
            self.assertFalse(catalog.BY_KEY[setting.page].live_only)


class DeviceTest(unittest.TestCase):
    def test_names(self):
        for name in ("pc", "alice-laptop", "A1", "x" * 63):
            self.assertTrue(device.valid_name(name), name)
            self.assertEqual(device.name_problem(name), "")
        self.assertIn("hyphen", device.name_problem("-pc"))
        self.assertIn("hyphen", device.name_problem("pc-"))
        self.assertIn("at most 63", device.name_problem("x" * 64))
        self.assertIn("no spaces", device.name_problem("my pc"))
        self.assertEqual(device.name_problem(""), "Enter a name.")
        self.assertEqual(device.suggest_name("Anna's PC"), "Annas-PC")
        self.assertEqual(device.suggest_name("  büro rechner "), "bro-rechner")

    def test_kind_of_device(self):
        self.assertEqual(device.chassis_kind("10"), "laptop")
        self.assertEqual(device.chassis_kind("3"), "desktop")
        self.assertEqual(device.chassis_kind("30"), "tablet")
        self.assertEqual(device.chassis_kind("1", "QEMU"), "vm")
        self.assertEqual(device.chassis_kind("x"), "")
        self.assertEqual(device.chassis_kind("3", machine_chassis="laptop"), "laptop")

    def test_read_device_and_specifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fx.make_root(Path(tmp))
            info = device.read_device(probes.Root(str(root)), uname_node="alice-laptop")
            self.assertEqual((info.name, info.kind, info.vendor, info.model),
                             ("alice-laptop", "laptop", "Example Computers", "Example Book 14"))
            self.assertEqual(info.model_text, "Example Computers Example Book 14")
            self.assertEqual(info.icons[0], "computer-laptop")
            (root / "sys/class/dmi/id/product_name").write_text("To Be Filled By O.E.M.\n")
            self.assertEqual(device.read_device(probes.Root(str(root))).model, "")
            (root / "etc/hostname").write_text("not a valid name!\n")
            self.assertEqual(device.read_device(probes.Root(str(root)), uname_node="running").name, "running")
            rows = dict(device.specifications(info, {"cpu_model": "CPU X", "cpu_threads": 8, "memory_bytes": 2 ** 33,
                                                     "machine": "x86_64", "boot_mode": "UEFI"}))
            self.assertEqual(rows["Processor"], "CPU X (8 threads)")
            self.assertEqual(rows["System type"], "x86_64, UEFI boot")
            self.assertNotIn("SECRET", repr(info))                  # product_serial is never read

    def test_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            account = device.read_account({"HOME": tmp})
            self.assertTrue(account.user)
            self.assertEqual(account.picture, "")
            Path(tmp, ".face").write_bytes(b"png")
            self.assertEqual(device.read_account({"HOME": tmp}).picture, os.path.join(tmp, ".face"))


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environ = fx.make_xdg(Path(self.tmp.name))
        self.home = Path(self.environ["HOME"])

    def test_reading(self):
        items = cleanup.cache_items(self.environ)
        self.assertEqual([i.name for i in items], ["mozilla", "pip"])
        self.assertEqual(items[0].size, 500000)
        self.assertEqual(items[0].files, 2)
        trash = cleanup.trash_usage(self.environ)
        self.assertEqual((trash.items, trash.size), (1, 50000))
        old = cleanup.old_downloads(self.environ, 30)
        self.assertEqual([i.name for i in old], ["installer-2025.iso"])        # today.pdf is new
        with self.assertRaises(ValueError):
            cleanup.old_downloads(self.environ, 7)
        (self.home / ".config").mkdir(exist_ok=True)
        (self.home / ".config/user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$HOME/Fetched"\n')
        self.assertEqual(cleanup.downloads_dir(self.environ), str(self.home / "Fetched"))
        self.assertEqual(cleanup.old_downloads(self.environ, 30), [])

    def test_links_are_not_followed(self):
        outside = Path(self.tmp.name) / "outside"
        (outside / "big").parent.mkdir(parents=True)
        (outside / "big").write_bytes(b"x" * 900000)
        os.symlink(outside, self.home / ".cache/link")
        self.assertNotIn("link", [i.name for i in cleanup.cache_items(self.environ)])   # counts as nothing
        userfiles.clear_cache_items(self.environ, ["link"])
        self.assertTrue((outside / "big").exists())                         # the link itself was removed
        self.assertFalse(os.path.lexists(self.home / ".cache/link"))

    def test_clearing_only_what_was_picked(self):
        freed = userfiles.clear_cache_items(self.environ, ["pip"])
        self.assertEqual(freed, 120000)
        self.assertFalse((self.home / ".cache/pip").exists())
        self.assertTrue((self.home / ".cache/mozilla").exists())
        for bad in ("../.local", "mozilla/firefox", "", ".", "..", "missing"):
            with self.assertRaises(userfiles.UserFileError, msg=bad):
                userfiles.clear_cache_items(self.environ, [bad])
        self.assertTrue((self.home / ".local/share/Trash").exists())

    def test_trash(self):
        freed = userfiles.empty_trash(self.environ)
        self.assertEqual(freed, 50000 + len("[Trash Info]\nPath=/home/alice/Documents/old%20report.odt\n"
                                            "DeletionDate=2026-11-01T10:00:00\n"))
        self.assertEqual(os.listdir(self.home / ".local/share/Trash/files"), [])
        self.assertEqual(os.listdir(self.home / ".local/share/Trash/info"), [])
        moved = userfiles.trash_downloads(self.environ, ["installer-2025.iso"])
        self.assertEqual(moved, 1)
        self.assertFalse((self.home / "Downloads/installer-2025.iso").exists())
        self.assertTrue((self.home / ".local/share/Trash/files/installer-2025.iso").exists())
        info = (self.home / ".local/share/Trash/info/installer-2025.iso.trashinfo").read_text()
        self.assertTrue(info.startswith("[Trash Info]\nPath="))
        self.assertIn("/Downloads/installer-2025.iso", info)
        self.assertRegex(info, r"DeletionDate=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        (self.home / "Downloads/installer-2025.iso").write_bytes(b"again")
        userfiles.trash_downloads(self.environ, ["installer-2025.iso"])      # a name taken in the Trash
        self.assertTrue((self.home / ".local/share/Trash/files/installer-2025.2.iso").exists())
        with self.assertRaises(userfiles.UserFileError):
            userfiles.trash_downloads(self.environ, ["../.bashrc"])

    def test_a_trash_that_is_a_link_is_left_alone(self):
        trash = self.home / ".local/share/Trash"
        target = Path(self.tmp.name) / "elsewhere"
        os.rename(trash, target)
        os.symlink(target, trash)
        with self.assertRaises(userfiles.UserFileError):
            userfiles.empty_trash(self.environ)
        self.assertTrue((target / "files/old report.odt").exists())


class FontFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environ = fx.make_xdg(Path(self.tmp.name))
        self.home = Path(self.environ["HOME"])

    def font(self, name: str, head: bytes = b"\x00\x01\x00\x00", size: int = 200) -> str:
        path = Path(self.tmp.name) / name
        path.write_bytes(head + b"\x00" * size)
        return str(path)

    def test_install_and_remove(self):
        path = userfiles.install_font(self.environ, self.font("My Font!.TTF"))
        self.assertEqual(path, str(self.home / ".local/share/fonts/My Font.ttf"))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        self.assertTrue(userfiles.install_font(self.environ, self.font("x.otf", b"OTTO")).endswith("x.otf"))
        self.assertTrue(userfiles.install_font(self.environ, self.font("c.ttc", b"ttcf")).endswith("c.ttc"))
        with self.assertRaises(userfiles.UserFileError):
            userfiles.install_font(self.environ, self.font("My Font!.TTF"))          # already installed
        for bad in (self.font("doc.ttf", b"%PDF"), self.font("tiny.ttf", size=10), "relative.ttf"):
            with self.assertRaises(userfiles.UserFileError, msg=bad):
                userfiles.install_font(self.environ, bad)
        link = Path(self.tmp.name) / "link.ttf"
        os.symlink(self.font("real.ttf"), link)
        with self.assertRaises(userfiles.UserFileError):
            userfiles.install_font(self.environ, str(link))
        self.assertTrue(userfiles.user_font(self.environ, path))
        userfiles.remove_font(self.environ, path)
        self.assertFalse(os.path.exists(path))
        for system in ("/usr/share/fonts/truetype/inter/Inter-Regular.ttf", str(self.home / ".bashrc"),
                       str(self.home / ".local/share/fonts/../fonts-other/x.ttf")):
            with self.assertRaises(userfiles.UserFileError, msg=system):
                userfiles.remove_font(self.environ, system)

    def test_font_list(self):
        families = fonts.parse_fc_list(fx.FONT_LIST.replace("{home}", str(self.home)),
                                       userfiles.font_dir(self.environ))
        self.assertEqual([f.name for f in families], ["Hack", "Handwriting Demo", "Inter", "Liberation Serif",
                                                      "Noto Sans"])
        inter = next(f for f in families if f.name == "Inter")
        self.assertEqual(inter.styles, ("Regular", "Bold"))
        self.assertFalse(inter.user)
        self.assertTrue(next(f for f in families if f.name == "Handwriting Demo").user)
        self.assertEqual([f.name for f in fonts.search(families, "SANS noto")], ["Noto Sans"])
        self.assertEqual(inter.summary, "2 styles · Comes with the system")


class TroubleshootTest(unittest.TestCase):
    def details(self, *, up=True, address=True, gateway=True, dns=True):
        from boswas_control_center import netinfo
        item = netinfo.Interface("wlp2s0", "Wi-Fi", "aa:bb:cc:dd:ee:ff", up, ("192.168.1.5/24",) if address else (),
                                 (), "192.168.1.1" if gateway else "")
        return netinfo.Details((item,), ("192.168.1.1",) if dns else ())

    def test_internet(self):
        good = troubleshoot.internet(self.details(), lambda host: ["151.101.2.132"], lambda url: 200)
        self.assertTrue(good.ok)
        self.assertEqual(good.headline, "No problem was found.")
        self.assertEqual(good.steps[-1].title, "Secure websites can be reached")
        for kwargs, headline, page in ((dict(up=False), "No network adapter is connected", "wifi"),
                                       (dict(address=False), "The connection has no address", "wifi"),
                                       (dict(gateway=False), "There is no route to the internet", "advanced-network"),
                                       (dict(dns=False), "No name server is set", "advanced-network")):
            report = troubleshoot.internet(self.details(**kwargs), lambda h: ["1.2.3.4"], lambda u: 200)
            self.assertFalse(report.ok)
            self.assertEqual(report.headline, headline)
            self.assertEqual(report.fix.page, page)

        def no_name(host):
            raise OSError(-2, "Name or service not known")

        def refused(url):
            raise OSError("Connection refused")

        self.assertEqual(troubleshoot.internet(self.details(), no_name, lambda u: 200).headline,
                         "Names on the internet are not found")
        self.assertEqual(troubleshoot.internet(self.details(), lambda h: ["1.2.3.4"], refused).headline,
                         "Secure websites cannot be reached")
        self.assertEqual(troubleshoot.internet(self.details(), lambda h: ["1.2.3.4"], lambda u: 511).headline,
                         "A secure website did not answer as expected")
        from boswas_control_center import netinfo
        self.assertEqual(troubleshoot.internet(netinfo.Details()).headline, "No network adapter was found")
        self.assertIn("deb.debian.org", troubleshoot.CONTACTED)
        with self.assertRaises(ValueError):
            troubleshoot.fetch_status("https://example.invalid/")             # only the address it says

    def test_sound_bluetooth_display(self):
        speakers = sb.AudioDevice(1, "Speakers", 0.6, False, True)
        state = sb.AudioState(True, speakers, None, (speakers,))
        self.assertTrue(troubleshoot.sound(sb.RUNNING, state).ok)
        muted = sb.AudioDevice(1, "Speakers", 0.6, True, True)
        self.assertEqual(troubleshoot.sound(sb.RUNNING, sb.AudioState(True, muted, None, (muted,))).headline,
                         "The sound is muted")
        self.assertEqual(troubleshoot.sound(sb.RUNNING, sb.AudioState(True, None)).headline,
                         "No speakers or headphones were found")
        self.assertEqual(troubleshoot.sound(sb.UNAVAILABLE, None).headline, "The sound server does not answer")
        on = sb.BluetoothState(True, True)
        self.assertTrue(troubleshoot.bluetooth("active", sb.RUNNING, on).ok)
        self.assertEqual(troubleshoot.bluetooth("inactive", sb.RUNNING, on).headline,
                         "The Bluetooth service is not running")
        self.assertEqual(troubleshoot.bluetooth("active", sb.RUNNING, sb.BluetoothState(True, False)).headline,
                         "Bluetooth is turned off")
        self.assertEqual(troubleshoot.bluetooth(None, sb.RUNNING, sb.BluetoothState(False)).headline,
                         "No Bluetooth adapter was found")
        from boswas_control_center import compositorconfig as cc
        monitor = cc.Output("eDP-1", True, mode=cc.Mode(1920, 1080, 60000))
        report = troubleshoot.display([monitor])
        self.assertTrue(report.ok)
        self.assertIn("1920 × 1080 at 60 Hz", report.steps[0].detail)
        self.assertEqual(troubleshoot.display([cc.Output("HDMI-A-1", False)]).headline, "Every monitor is turned off")
        self.assertEqual(troubleshoot.display([]).headline, "No monitor is connected")
        self.assertEqual(troubleshoot.display(None, "no socket").headline, "The desktop did not report its monitors")


class PeripheralsTest(unittest.TestCase):
    def test_cameras_and_usb(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = probes.Root(str(fx.make_root(Path(tmp))))
            cameras = peripherals.cameras(root)
            self.assertEqual([(c.node, c.name, c.bus) for c in cameras],
                             [("video0", "Integrated Camera: Integrated C", "USB")])     # the metadata node is not
            self.assertEqual([d.name for d in peripherals.usb(root)], ["USB Keyboard"])
            self.assertTrue(peripherals.cups_installed(root))

    def test_drives_on_usb_and_how_they_are_ejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = fx.make_root(Path(tmp))
            fx.add_usb_stick(base)
            root = probes.Root(str(base))
            self.assertEqual([d.name for d in peripherals.usb(root)], ["USB Keyboard", "Ultra Fit"])
            drives = peripherals.usb_drives(root)
            self.assertEqual(drives, [peripherals.Drive("/dev/sdb", "1-4", "SanDisk Ultra Fit", 61341696 * 512,
                                                        (("/dev/sdb1", "/media/alice/USB STICK"),))])
            self.assertFalse(drives[0].in_use_by_system)
            self.assertEqual(drives[0].folder, "/media/alice/USB STICK")
            self.assertEqual(peripherals.eject_steps(drives[0]), [("unmount", "/dev/sdb1"), ("power-off", "/dev/sdb")])
        with tempfile.TemporaryDirectory() as tmp:
            base = fx.make_root(Path(tmp))
            fx.add_usb_stick(base, encrypted=True)
            drive = peripherals.usb_drives(probes.Root(str(base)))[0]
            self.assertEqual((drive.mounts, drive.unlocked), ((("/dev/dm-0", "/media/alice/USB STICK"),), ("/dev/sdb1",)))
            self.assertEqual(peripherals.eject_steps(drive),
                             [("unmount", "/dev/dm-0"), ("lock", "/dev/sdb1"), ("power-off", "/dev/sdb")])
        with tempfile.TemporaryDirectory() as tmp:
            base = fx.make_root(Path(tmp))
            fx.add_usb_stick(base, mounted=None)
            drive = peripherals.usb_drives(probes.Root(str(base)))[0]
            self.assertEqual((drive.mounts, drive.folder), ((), "computer:///"))
            self.assertEqual(peripherals.eject_steps(drive), [("power-off", "/dev/sdb")])
        with tempfile.TemporaryDirectory() as tmp:
            base = fx.make_root(Path(tmp))
            fx.add_usb_stick(base, mounted="/run/live/medium")                # the boot medium of a live session
            drive = peripherals.usb_drives(probes.Root(str(base)))[0]
            self.assertTrue(drive.in_use_by_system)
            with self.assertRaises(ValueError):
                peripherals.eject_steps(drive)
            self.assertEqual(peripherals.usb_drives(probes.Root(str(Path(tmp) / "nothing"))), [])

    def test_mountinfo(self):
        found = peripherals.parse_mountinfo("36 35 98:0 /mnt1 /media/a/My\\040Disk rw - ext3 /dev/x rw\n"
                                            "bad line\n37 35 x:y / /z rw - tmpfs t rw\n38 35 8:17 / relative rw - a b\n")
        self.assertEqual(found, {"98:0": ["/media/a/My Disk"]})

    def test_printers(self):
        names = peripherals.parse_destinations(fx.LPSTAT_E + "-bad\nbad name\n")
        self.assertEqual(names, ["Office_Laser", "Home_Inkjet"])
        printers = peripherals.parse_printers(fx.LPSTAT_PD, names)
        self.assertEqual([(p.name, p.state, p.enabled, p.default) for p in printers],
                         [("Office_Laser", "Stopped", False, True), ("Home_Inkjet", "Ready", True, False)])
        self.assertEqual(peripherals.parse_printers("printer A now printing A-1.  enabled since x\n"),
                         [peripherals.Printer("A", "Printing", True, False)])


class NetconfigTest(unittest.TestCase):
    def test_connections(self):
        found = netconfig.parse_connections(fx.NMCLI_CONNECTIONS)
        self.assertEqual([(c.name, c.kind, c.active) for c in found],
                         [("wg-home", "wireguard", True), ("Office VPN", "vpn", False)])
        self.assertEqual(found[0].kind_label, "WireGuard")
        self.assertTrue(netconfig.needs_secrets("Error: Connection activation failed: Secrets were required, but "
                                                "not provided"))

    def test_import_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            wg = Path(tmp, "wg-office.conf")
            wg.write_text("[Interface]\nPrivateKey = abc=\nAddress = 10.0.0.2/32\n[Peer]\nPublicKey = x\n")
            self.assertEqual(netconfig.import_kind(str(wg)), ("wireguard", ""))
            long_name = Path(tmp, "my-office-wireguard.conf")
            long_name.write_text(wg.read_text())
            kind, why = netconfig.import_kind(str(long_name))
            self.assertEqual(kind, "")
            self.assertIn("15 letters", why)
            ovpn = Path(tmp, "office.ovpn")
            ovpn.write_text("client\ndev tun\nremote vpn.example.invalid 1194\n<ca>\n</ca>\n")
            self.assertEqual(netconfig.import_kind(str(ovpn)), ("openvpn", ""))
            other = Path(tmp, "notes.conf")
            other.write_text("[Section]\nkey=value\n")
            self.assertEqual(netconfig.import_kind(str(other))[0], "")
            self.assertEqual(netconfig.import_kind(tmp)[0], "")                  # a folder
            link = Path(tmp, "link.ovpn")
            os.symlink(ovpn, link)
            self.assertEqual(netconfig.import_kind(str(link))[0], "")            # not through a link

    def test_wired_connections_and_their_settings(self):
        connections = netconfig.parse_wired_connections(fx.NMCLI_WIRED_CONNECTIONS)
        self.assertEqual([(c.name, c.device, c.active) for c in connections],
                         [("Wired connection 1", "enp3s0", True), ("Spare", "", False)], "VPNs are not wired")
        self.assertEqual(netconfig.profile_for("enp3s0", connections).uuid, fx.WIRED_UUID)
        self.assertEqual(netconfig.profile_for("enp9s0", connections).name, "Spare", "one not active elsewhere")
        self.assertIsNone(netconfig.profile_for("x", connections[:1]))
        auto = netconfig.parse_wired_settings(fx.NMCLI_WIRED_SETTINGS)
        self.assertEqual((auto.uuid, auto.name, auto.method, auto.addresses, auto.gateway, auto.dns, auto.metered),
                         (fx.WIRED_UUID, "Wired connection 1", "auto", (), "", (), "unknown"))
        self.assertEqual((netconfig.ip_assignment_text(auto), netconfig.dns_assignment_text(auto)),
                         ("Automatic (DHCP)", "Automatic (DHCP)"))
        manual = netconfig.parse_wired_settings(
            "connection.metered:yes (guessed)\nipv4.method:manual\nipv4.addresses:192.168.1.20/24,10.0.0.5/8\n"
            "ipv4.gateway:192.168.1.1\nipv4.dns:1.1.1.1,9.9.9.9\nipv4.ignore-auto-dns:yes\n"
            "ipv6.dns:2606\\:4700\\:4700\\:\\:1111\nnot a field\nipv4.addresses:junk\n", fx.WIRED_UUID, "Desk")
        self.assertEqual((manual.uuid, manual.name, manual.addresses, manual.dns, manual.metered),
                         (fx.WIRED_UUID, "Desk", ("192.168.1.20/24", "10.0.0.5/8"),
                          ("1.1.1.1", "9.9.9.9", "2606:4700:4700::1111"), "yes"))
        self.assertEqual(netconfig.ip_assignment_text(manual),
                         "Manual: 192.168.1.20/24, 10.0.0.5/8 · gateway 192.168.1.1")
        self.assertEqual(netconfig.dns_assignment_text(manual), "Manual: 1.1.1.1, 9.9.9.9, 2606:4700:4700::1111")
        extra = netconfig.parse_wired_settings("ipv4.method:auto\nipv4.dns:8.8.8.8\nipv4.ignore-auto-dns:no\n")
        self.assertEqual(netconfig.dns_assignment_text(extra), "Manual: 8.8.8.8 (and the network's)")
        details = netconfig.parse_device_details(fx.NMCLI_WIRED_DEVICE, 1000)
        self.assertEqual(netconfig.detail_rows(details),
                         [("Link speed", "1 Gb/s"), ("IPv4 address", "192.168.1.23/24"), ("IPv4 gateway", "192.168.1.1"),
                          ("IPv6 address", "fe80::5054:ff:fe12:3456/64"), ("DNS servers", "192.168.1.1"),
                          ("Physical address (MAC)", "52:54:00:12:34:56")])
        self.assertEqual(netconfig.detail_rows(netconfig.parse_device_details("")), [])
        with tempfile.TemporaryDirectory() as tmp:
            root = probes.Root(str(fx.make_root(Path(tmp))))
            self.assertEqual((netconfig.link_speed(root, "enp3s0"), netconfig.link_speed(root, "../x")), (1000, None))

    def test_wired_changes_are_validated_before_nmcli(self):
        self.assertEqual(netconfig.ip_words(False), ("ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", ""))
        self.assertEqual(netconfig.ip_words(True, " 192.168.1.20 ", "24", "192.168.1.1", ["1.1.1.1", "2606:4700::1111"]),
                         ("ipv4.method", "manual", "ipv4.addresses", "192.168.1.20/24", "ipv4.gateway", "192.168.1.1",
                          "ipv4.dns", "1.1.1.1", "ipv4.ignore-auto-dns", "yes", "ipv6.dns", "2606:4700::1111",
                          "ipv6.ignore-auto-dns", "yes"))
        self.assertEqual(netconfig.ip_words(True, "10.0.0.2", "8", "")[:6],
                         ("ipv4.method", "manual", "ipv4.addresses", "10.0.0.2/8", "ipv4.gateway", ""))
        for address, prefix, gateway, words in (("192.168.1", "24", "", "IPv4 address"), ("192.168.1.20", "33", "", "1 to 32"),
                                                ("192.168.1.0", "24", "", "network's own"),
                                                ("192.168.1.20", "24", "10.0.0.1", "same network"),
                                                ("192.168.1.20", "24", "192.168.1.20", "own address"),
                                                ("192.168.1.20", "24", "router", "IPv4 address")):
            self.assertIn(words, netconfig.ipv4_problem(address, prefix, gateway), (address, prefix, gateway))
            with self.assertRaises(ValueError):
                netconfig.ip_words(True, address, prefix, gateway)
        self.assertEqual(netconfig.dns_words(None)[1::2], ("", "no", "", "no"))
        self.assertEqual(netconfig.dns_words(["9.9.9.9", "2001:DB8::53"]),
                         ("ipv4.dns", "9.9.9.9", "ipv4.ignore-auto-dns", "yes", "ipv6.dns", "2001:db8::53",
                          "ipv6.ignore-auto-dns", "yes"))
        for servers, words in ((["dns.example"], "not an IPv4 or IPv6"), (["1.1.1.1", "1.1.1.1"], "twice"),
                               (["1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4"], "At most 3"), ([" "], "at least one")):
            self.assertIn(words, netconfig.dns_problem(servers), servers)
        self.assertEqual(netconfig.metered_words(True), ("connection.metered", "yes"))
        for words in (netconfig.ip_words(False), netconfig.dns_words(["1.1.1.1"]), netconfig.metered_words(False)):
            commands._check(commands.nmcli_change_argv("connection", "modify", "uuid", fx.WIRED_UUID, *words))

    def test_counters_and_hardware_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = probes.Root(str(fx.make_root(Path(tmp))))
            traffic = netconfig.traffic(root, "wlp2s0")
            self.assertEqual((traffic.received, traffic.sent), (1536000000, 204800000))
            self.assertIn("Received 1.4 GiB", netconfig.traffic_text(traffic))
            self.assertEqual(netconfig.hardware_address(root, "wlp2s0"), "a0:b1:c2:d3:e4:f5")
            self.assertEqual(netconfig.hardware_address(root, "../x"), "")
            self.assertEqual(netconfig.traffic(root, "missing"), netconfig.Traffic(None, None))


class InstalledAppsTest(unittest.TestCase):
    def test_owners_and_assembly(self):
        owners = installed.parse_owners("kate: /usr/share/applications/org.kde.kate.desktop\n"
                                        "libreoffice-core, libreoffice-common: /usr/share/applications/lo.desktop\n"
                                        "diversion by x from: /usr/bin/y\nnot a line\n")
        self.assertEqual(owners, {"/usr/share/applications/org.kde.kate.desktop": "kate",
                                  "/usr/share/applications/lo.desktop": "libreoffice-core"})
        entry = vm.DesktopEntry
        entries = [entry("org.kde.kate", "/usr/share/applications/org.kde.kate.desktop", "Kate"),
                   entry("boswas-files", "/usr/share/applications/boswas-files.desktop", "Files"),
                   entry("org.gimp.GIMP", "/h/.local/share/flatpak/exports/share/applications/org.gimp.GIMP.desktop",
                         "GIMP"),
                   entry("org.x.Sys", "/var/lib/flatpak/exports/share/applications/org.x.Sys.desktop", "Sys"),
                   entry("boswas-appimage-krita", "/h/.local/share/applications/boswas-appimage-krita.desktop",
                         "Krita"),
                   entry("custom", "/h/.local/share/applications/custom.desktop", "Custom"),
                   entry("boswas-winapp-x", "/h/.local/share/applications/boswas-winapp-x.desktop", "X",
                         winapp_id="com.example.x")]
        self.assertEqual(installed.debian_paths(entries, "/h"), ["/usr/share/applications/org.kde.kate.desktop",
                                                                 "/usr/share/applications/boswas-files.desktop"])
        from boswas_control_center import software
        apps = installed.assemble(entries, {entries[0].path: "kate", entries[1].path: "boswas-apps"},
                                  {"kate": ("ii ", "24.12", 9216000)},
                                  [software.FlatpakApp("org.gimp.GIMP", "GIMP", "", "3.0", "flathub", "user")],
                                  [userfiles.AppImage("krita", "Krita", "/h/Applications/krita.AppImage", "", 5000)],
                                  [{"id": "com.example.x", "name": "Example X", "version": "2"}], "/h")
        by = {a.name: a for a in apps}
        self.assertEqual((by["Kate"].source, by["Kate"].package, by["Kate"].removable), ("debian", "kate", True))
        self.assertIn("8.8 MiB", by["Kate"].detail)
        self.assertFalse(by["Files"].removable)
        self.assertEqual(by["Files"].why_not, "A part of Boswas OS.")
        self.assertEqual((by["GIMP"].source, by["GIMP"].installation, by["GIMP"].removable), ("flatpak", "user", True))
        self.assertFalse(by["Sys"].removable)
        self.assertEqual((by["Krita"].source, by["Krita"].appimage_id), ("appimage", "krita"))
        self.assertEqual(by["Custom"].source, "other")
        self.assertEqual(by["Example X"].source, "windows")
        self.assertNotIn("X", by)                                           # the launcher of a Windows app
        self.assertEqual([a.name for a in installed.view(apps, source="flatpak")], ["GIMP", "Sys"])
        self.assertEqual(installed.view(apps, order="size")[0].name, "Kate")
        self.assertEqual(installed.view(apps, order="name-desc")[0].name, "Sys")
        self.assertEqual([a.name for a in installed.view(apps, "kat")], ["Kate"])


class FeaturesTest(unittest.TestCase):
    def test_features(self):
        names = {package for feature in features.FEATURES for package in feature.packages}
        for name in names:
            self.assertTrue(commands.valid_package(name), name)
        self.assertIn("7zip", names)
        policy = (fx.PACKAGE.parent / "boswas-sysops" / "boswas_sysops" / "policy.py").read_text()
        for name in names:
            self.assertNotIn(f'"{name}"', policy, f"{name} is excluded or protected by sysops")
        by_key = {feature.key: feature for feature in features.FEATURES}
        for key in ("printing", "pdf-printer"):                 # they bring cups-daemon, which recommends cups-browsed
            self.assertEqual(by_key[key].install_words(("cups",))[:2], ("apt-install", "--no-recommends"), key)
            commands.sysops_argv(*by_key[key].install_words(by_key[key].packages))
        self.assertEqual(by_key["7zip"].install_words(("7zip",)), ("apt-install", "7zip"))
        self.assertIn('"cups-browsed"', policy, "the reason for --no-recommends")
        self.assertEqual(peripherals.PRINTING_INSTALL, ("apt-install", "--no-recommends", "cups"))
        languages = features.language_features(["C.utf8", "de_DE.utf8", "de_AT.utf8", "pt_BR.utf8", "en_US.utf8",
                                                "xx_YY.utf8"])
        self.assertEqual([(f.title, f.packages) for f in languages],
                         [("German for Firefox", ("firefox-esr-l10n-de",)),
                          ("Portuguese (Brazil) for Firefox", ("firefox-esr-l10n-pt-br",))])
        status = features.parse_status("7zip\tii \t6000\t24.09\ncups\tun \t\t\nbad line\n")
        self.assertEqual(status["7zip"], ("ii", "24.09", 6000 * 1024))
        self.assertTrue(features.installed(status, "7zip"))
        self.assertFalse(features.installed(status, "cups"))
        office = next(f for f in features.FEATURES if f.key == "office-fonts")
        self.assertEqual(features.feature_state(office, {"fonts-crosextra-carlito": ("ii ", "", None)}), "partly")


class PermissionsTest(unittest.TestCase):
    def test_permission_store(self):
        found = permissions.parse_list("Table\tObject\tApp\tPermissions\tData\n" + fx.PERMISSIONS)
        self.assertEqual([(p.title, p.app, p.value) for p in found],
                         [("Camera", "org.example.Meet", "Allowed"),
                          ("Location", "org.example.Maps", "Allowed (exact accuracy)"),
                          ("Screenshots", "org.example.Draw", "Denied"),
                          ("Run in the background", "org.example.Meet", "Allowed")])   # the line without an app
        self.assertEqual(list(permissions.by_app(found)), ["org.example.Draw", "org.example.Maps", "org.example.Meet"])
        self.assertEqual(permissions.Permission("x-table", "y", "a.b.C", "Allowed").title, "x-table: y")
        self.assertIn("not sandboxed", permissions.NOT_SANDBOXED)


class UpdateHistoryTest(unittest.TestCase):
    def test_parse(self):
        entries = updatelog.parse(fx.HISTORY_LOG)
        self.assertEqual([e.start for e in entries], ["2026-11-01 10:00:00", "2026-11-03 06:12:01"])
        install, upgrade = entries
        self.assertEqual(install.summary, "Installed 2 packages")
        self.assertEqual(install.by, "Requested by alice")
        self.assertTrue(install.changes["Install"][1].automatic)
        self.assertTrue(upgrade.automatic)
        self.assertEqual(upgrade.by, "Automatic security updates")
        self.assertEqual(upgrade.changes["Upgrade"][0].text, "libssl3t64 (3.5.1-1 → 3.5.1-1+deb13u1)")
        self.assertEqual(updatelog.parse("garbage\n\nStart-Date: yesterday\n"), [])

    def test_read_with_rotated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fx.make_root(Path(tmp))
            history = updatelog.read(probes.Root(str(root)))
            self.assertTrue(history.found and history.readable)
            self.assertEqual([e.summary for e in history.entries],
                             ["Updated 2 packages", "Installed 2 packages", "Removed 1 package"])   # newest first
            (root / "var/log/apt/history.log.1.gz").write_bytes(b"not gzip")
            self.assertEqual(len(updatelog.read(probes.Root(str(root))).entries), 2)
            os.unlink(root / "var/log/apt/history.log")
            os.unlink(root / "var/log/apt/history.log.1.gz")
            self.assertFalse(updatelog.read(probes.Root(str(root))).found)
            if os.geteuid() != 0:                               # root reads everything: the check needs a user
                path = root / "var/log/apt/history.log"
                path.write_text(fx.HISTORY_LOG)
                os.chmod(path, 0)
                self.assertFalse(updatelog.read(probes.Root(str(root))).readable)

    def test_update_option_rows(self):
        rows = dict(vm.update_option_rows({"periodic": {"Download-Upgradeable-Packages": "1",
                                                        "AutocleanInterval": "7"}}))
        self.assertEqual(rows, {"Download updates in advance": "Every day",
                                "Remove outdated downloaded packages": "Every 7 days"})
        self.assertEqual(dict(vm.update_option_rows({}))["Download updates in advance"], "Unknown")


if __name__ == "__main__":
    unittest.main()
