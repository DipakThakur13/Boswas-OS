"""mountinfo, the list of volumes, reading UDisks2 through a fake bus."""

import os
import unittest

import support  # noqa: F401

from boswas_apps.files import volumes
from boswas_apps.files.volumes import BLOCK, BLOCK_DEVICES, DRIVE, Mount, UDisksReader

MOUNTINFO = r"""
22 28 0:21 / /sys rw,nosuid,nodev,noexec,relatime shared:7 - sysfs sysfs rw
23 28 0:22 / /proc rw,nosuid,nodev,noexec,relatime shared:14 - proc proc rw
24 28 0:5 / /dev rw,nosuid shared:2 - devtmpfs udev rw,size=8G
28 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw,errors=remount-ro
30 28 259:1 / /boot/efi rw,relatime shared:30 - vfat /dev/nvme0n1p1 rw,fmask=0077
31 28 259:3 / /home rw,relatime shared:31 - ext4 /dev/nvme0n1p3 rw
40 28 0:44 / /run/user/1000 rw,nosuid,nodev - tmpfs tmpfs rw,size=1G
41 28 8:17 / /media/me/USB\040STICK rw,nosuid,nodev,relatime shared:40 - vfat /dev/sdb1 rw,uid=1000
42 28 8:33 / /media/me/tab\011and\134slash ro,relatime - iso9660 /dev/sdc1 ro
43 28 0:50 / /mnt/share rw,relatime - cifs //server/share rw,vers=3.0
44 28 7:0 / /snap/core/1 ro,nodev,relatime - squashfs /dev/loop0 ro
45 28 0:51 / /home/me/.cache/doc rw,nosuid,nodev - fuse.portal portal rw
46 28 253:0 / /data rw,relatime master:5 propagate_from:2 unbindable - btrfs /dev/mapper/vault rw
47 28 8:17 /sub /srv/bind rw - vfat /dev/sdb1 rw
this line is garbage
48 28 x:y / /bad rw - ext4 /dev/bad rw
49 28 8:99 / relative rw - ext4 /dev/rel rw
"""


class MountinfoTest(unittest.TestCase):
    def setUp(self):
        self.mounts = volumes.parse_mountinfo(MOUNTINFO)

    def test_parse(self):
        by_point = {mount.mount_point: mount for mount in self.mounts}
        self.assertEqual(len(self.mounts), 14)
        self.assertEqual(by_point["/"], Mount("/", "/dev/nvme0n1p2", "ext4", (259, 2), "rw,relatime"))
        self.assertIn("/media/me/USB STICK", by_point)
        self.assertIn("/media/me/tab\tand\\slash", by_point)
        self.assertTrue(by_point["/media/me/tab\tand\\slash"].read_only)
        self.assertFalse(by_point["/"].read_only)
        self.assertEqual(by_point["/data"].fstype, "btrfs")
        self.assertEqual(by_point["/data"].source, "/dev/mapper/vault")
        self.assertEqual(by_point["/srv/bind"].root, "/sub")
        self.assertNotIn("/bad", by_point)

    def test_garbage(self):
        self.assertEqual(volumes.parse_mountinfo(""), [])
        self.assertEqual(volumes.parse_mountinfo("\0\0\0\n- - - -\n1 2 3\n" + "x " * 5000), [])
        self.assertEqual(volumes.read_mounts("/no/such/file"), [])
        self.assertTrue(volumes.read_mounts())             # the real one of this machine parses

    def test_displayable(self):
        shown = [mount.mount_point for mount in self.mounts if volumes.is_displayable(mount, "/home/me")]
        self.assertEqual(shown, ["/", "/home", "/media/me/USB STICK", "/media/me/tab\tand\\slash", "/mnt/share",
                                 "/data"])

    def test_effective_and_mount_of(self):
        doubled = self.mounts + [Mount("/home", "/dev/sdz9", "xfs", (8, 250))]
        effective = volumes.effective(doubled)
        self.assertEqual(len(effective), len(self.mounts))
        self.assertEqual(volumes.mount_of("/home/me/file", doubled).source, "/dev/sdz9")
        self.assertEqual(volumes.mount_of("/media/me/USB STICK/x/y", self.mounts).source, "/dev/sdb1")
        self.assertEqual(volumes.mount_of("/homework", self.mounts).mount_point, "/")
        self.assertEqual(volumes.mount_of("/", self.mounts).mount_point, "/")
        self.assertIsNone(volumes.mount_of("/x", []))

    def test_usage_and_labels(self):
        total, free = volumes.usage("/")
        self.assertGreater(total, 0)
        self.assertLessEqual(free, total)
        self.assertIsNone(volumes.usage("/no/such/place"))
        self.assertEqual(volumes.labels("/no/such/dir"), {})


class Tmp(support.TempCase):
    def test_labels_from_links(self):
        devices = self.mkdir("dev")
        open(os.path.join(devices, "sdb1"), "w").close()
        by_label = self.mkdir("dev/disk/by-label")
        os.symlink("../../sdb1", os.path.join(by_label, r"USB\x20STICK"))
        os.symlink("../../gone", os.path.join(by_label, "Orphan"))
        self.assertEqual(volumes.labels(by_label)[os.path.join(devices, "sdb1")], "USB STICK")


def fake_udisks():
    """A UDisks2 like a laptop with an encrypted data volume, a USB stick, a DVD and a locked stick."""
    root = BLOCK_DEVICES
    drives = "/org/freedesktop/UDisks2/drives"
    blocks = {
        f"{root}/nvme0n1p1": {"IdUsage": "filesystem", "HintIgnore": True, "Device": b"/dev/nvme0n1p1\0"},
        f"{root}/nvme0n1p2": {"IdUsage": "filesystem", "HintIgnore": False, "Device": b"/dev/nvme0n1p2\0",
                              "IdLabel": "root", "IdType": "ext4", "Size": 100 * 2 ** 30, "Drive": f"{drives}/nvme",
                              "HintSystem": True, "CryptoBackingDevice": "/"},
        f"{root}/nvme0n1p3": {"IdUsage": "filesystem", "HintIgnore": False, "Device": b"/dev/nvme0n1p3\0",
                              "IdLabel": "", "IdType": "ext4", "Size": 200 * 2 ** 30, "Drive": f"{drives}/nvme",
                              "HintSystem": True, "CryptoBackingDevice": "/"},
        f"{root}/nvme0n1p4": {"IdUsage": "crypto", "HintIgnore": False, "Device": b"/dev/nvme0n1p4\0",
                              "IdType": "crypto_LUKS", "Size": 50 * 2 ** 30, "Drive": f"{drives}/nvme",
                              "HintSystem": True, "CryptoBackingDevice": "/"},
        f"{root}/dm_2d0": {"IdUsage": "filesystem", "HintIgnore": False, "Device": b"/dev/dm-0\0",
                           "IdLabel": "Vault", "IdType": "btrfs", "Size": 50 * 2 ** 30, "Drive": "/",
                           "HintSystem": True, "CryptoBackingDevice": f"{root}/nvme0n1p4"},
        f"{root}/nvme0n1p5": {"IdUsage": "other", "HintIgnore": False, "Device": b"/dev/nvme0n1p5\0"},
        f"{root}/nvme0n1": {"IdUsage": "", "HintIgnore": False},
        f"{root}/sdb1": {"IdUsage": "filesystem", "HintIgnore": False, "Device": b"/dev/sdb1\0",
                         "IdLabel": "USB STICK", "IdType": "vfat", "Size": 16 * 2 ** 30, "Drive": f"{drives}/stick",
                         "HintSystem": False, "CryptoBackingDevice": "/"},
        f"{root}/sdc1": {"IdUsage": "filesystem", "HintIgnore": False, "Device": b"/dev/sdc1\0",
                         "IdLabel": "MOVIE", "IdType": "iso9660", "Size": 4 * 2 ** 30, "Drive": f"{drives}/dvd"},
        f"{root}/sdd1": {"IdUsage": "filesystem", "HintIgnore": False, "Device": b"/dev/sdd1\0",
                         "IdLabel": "", "IdType": "ext4", "Size": 32 * 2 ** 30, "Drive": f"{drives}/stick2"},
        f"{root}/sdd2": {"IdUsage": "crypto", "HintIgnore": False, "Device": b"/dev/sdd2\0",
                         "IdLabel": "", "IdType": "crypto_LUKS", "Size": 8 * 2 ** 30, "Drive": f"{drives}/stick2"},
        f"{root}/loop0": {"IdUsage": "filesystem", "HintIgnore": False, "Device": b"/dev/loop0\0",
                          "IdType": "squashfs", "Size": 2 ** 20, "Drive": "/"},
    }
    drive_props = {
        f"{drives}/nvme": {"Removable": False, "MediaRemovable": False, "Ejectable": False, "Optical": False,
                           "ConnectionBus": "", "Vendor": "", "Model": "FastDisk 1TB", "CanPowerOff": False},
        f"{drives}/stick": {"Removable": True, "MediaRemovable": True, "Ejectable": True, "Optical": False,
                            "ConnectionBus": "usb", "Vendor": "ACME", "Model": "Stick", "CanPowerOff": True},
        f"{drives}/dvd": {"Removable": True, "MediaRemovable": True, "Ejectable": True, "Optical": True,
                          "ConnectionBus": "usb", "Vendor": "ACME", "Model": "DVD", "CanPowerOff": False},
        f"{drives}/stick2": {"Removable": True, "MediaRemovable": False, "Ejectable": False, "Optical": False,
                             "ConnectionBus": "usb", "Vendor": "", "Model": "", "CanPowerOff": True},
    }
    return blocks, drive_props


class FakeBus:
    """Answers Introspect and Properties.Get from dictionaries, at once or when ``flush`` is called."""

    def __init__(self, blocks, drives, deferred=False, missing=False):
        self.blocks, self.drives, self.deferred, self.missing = blocks, drives, deferred, missing
        self.queue = []
        self.calls = []

    def call(self, path, interface, method, arguments, callback):
        self.calls.append((path, interface, method, tuple(arguments)))
        answer = self.answer(path, interface, method, arguments)
        if self.deferred:
            self.queue.append((callback, answer))
        else:
            callback(*answer)

    def answer(self, path, interface, method, arguments):
        if self.missing:
            return False, ["org.freedesktop.DBus.Error.ServiceUnknown", "no such service"]
        if method == "Introspect":
            nodes = "".join(f'  <node name="{name.rsplit("/", 1)[1]}"/>\n' for name in self.blocks)
            return True, [f"<node>\n<interface name='x'/>\n{nodes}<node name=\"../evil\"/></node>"]
        kind, name = arguments
        source = self.blocks if kind == BLOCK else self.drives if kind == DRIVE else {}
        props = source.get(path, {})
        if name not in props:
            return False, ["org.freedesktop.DBus.Error.UnknownProperty", name]
        return True, [props[name]]

    def flush(self):
        while self.queue:
            callback, answer = self.queue.pop(0)
            callback(*answer)


class UDisksReaderTest(unittest.TestCase):
    def read(self, **kwargs):
        blocks, drives = fake_udisks()
        bus = FakeBus(blocks, drives, **kwargs)
        results = []
        UDisksReader(bus.call).read(results.append)
        bus.flush()
        return bus, results

    def test_reads_filesystems_and_their_drives(self):
        for deferred in (False, True):
            bus, results = self.read(deferred=deferred)
            self.assertEqual(len(results), 1, deferred)
            data = results[0]
            names = sorted(path.rsplit("/", 1)[1] for path in data["blocks"])
            self.assertEqual(names, ["dm_2d0", "loop0", "nvme0n1p2", "nvme0n1p3", "nvme0n1p4", "sdb1", "sdc1", "sdd1",
                                     "sdd2"])
            self.assertEqual(sorted(path.rsplit("/", 1)[1] for path in data["drives"]),
                             ["dvd", "nvme", "stick", "stick2"])
            stick = data["blocks"][f"{BLOCK_DEVICES}/sdb1"]
            self.assertEqual((stick["Device"], stick["IdLabel"], stick["Size"]), (b"/dev/sdb1\0", "USB STICK", 2 ** 34))
            self.assertNotIn("HintName", stick)                 # a property UDisks did not have
            self.assertTrue(data["drives"]["/org/freedesktop/UDisks2/drives/stick"]["Ejectable"])
            self.assertFalse(any("evil" in call[0] for call in bus.calls))
            # the ignored partition and the bare disk were asked two properties each, no more
            self.assertEqual(sum(1 for call in bus.calls if call[0].endswith("/nvme0n1p1")), 2)
            self.assertEqual(sum(1 for call in bus.calls if call[0].endswith("/nvme0n1")), 2)
            self.assertEqual(sum(1 for call in bus.calls if call[0].endswith("/drives/nvme")), 8)

    def test_no_service(self):
        for deferred in (False, True):
            _bus, results = self.read(missing=True, deferred=deferred)
            self.assertEqual(results, [None])

    def test_no_devices(self):
        bus = FakeBus({}, {})
        results = []
        UDisksReader(bus.call).read(results.append)
        self.assertEqual(results, [{"blocks": {}, "drives": {}}])

    def test_hostile_values(self):
        blocks = {f"{BLOCK_DEVICES}/sdx": {"IdUsage": "filesystem", "HintIgnore": False, "Device": object(),
                                           "Drive": "/../../etc", "Size": "big", "IdLabel": ["list"]}}
        bus = FakeBus(blocks, {})
        results = []
        UDisksReader(bus.call).read(results.append)
        self.assertEqual(results[0]["drives"], {})
        self.assertIsInstance(results[0]["blocks"][f"{BLOCK_DEVICES}/sdx"]["Device"], str)


class AssembleTest(unittest.TestCase):
    DEVICES = {"/dev/nvme0n1p1": (259, 1), "/dev/nvme0n1p2": (259, 2), "/dev/nvme0n1p3": (259, 3),
               "/dev/nvme0n1p4": (259, 4), "/dev/dm-0": (253, 0), "/dev/sdb1": (8, 17), "/dev/sdc1": (8, 33),
               "/dev/sdd1": (8, 49), "/dev/sdd2": (8, 50), "/dev/loop0": (7, 0)}

    def assemble(self, udisks):
        mounts = volumes.parse_mountinfo(MOUNTINFO)
        return volumes.assemble(mounts, udisks, user_home="/home/me",
                                usage_of=lambda point: (1000 * 2 ** 30, 250 * 2 ** 30),
                                label_of={"/dev/nvme0n1p3": "HomeLabel"}, devnum_of=self.DEVICES.get)

    def test_without_udisks_everything_is_read_only_information(self):
        result = self.assemble(None)
        self.assertEqual([(volume.name, volume.kind, volume.mount_point) for volume in result],
                         [("File system", "root", "/"), ("data", "disk", "/data"), ("HomeLabel", "disk", "/home"),
                          ("tab\tand\\slash", "disk", "/media/me/tab\tand\\slash"),
                          ("USB STICK", "disk", "/media/me/USB STICK"), ("share", "network", "/mnt/share")])
        for volume in result:
            self.assertFalse(volume.can_mount or volume.can_unmount or volume.can_unlock or volume.can_eject)
            self.assertEqual(volume.space_text, "250 GB free of 1000 GB")
        self.assertEqual(result[0].used, 750 * 2 ** 30)
        self.assertTrue(result[3].read_only)

    def test_with_udisks(self):
        blocks, drives = fake_udisks()
        result = {volume.name: volume for volume in self.assemble({"blocks": blocks, "drives": drives})}
        self.assertEqual(sorted(result), ["32.0 GB volume", "8.00 GB encrypted volume", "File system", "HomeLabel",
                                          "MOVIE", "USB STICK", "Vault", "share"])
        root = result["File system"]
        self.assertEqual((root.kind, root.can_unmount, root.can_eject, root.description),
                         ("root", False, False, "FastDisk 1TB"))
        stick = result["USB STICK"]
        self.assertEqual((stick.kind, stick.can_unmount, stick.can_eject, stick.can_power_off, stick.mounted),
                         ("removable", True, True, True, True))
        self.assertEqual(stick.object_path, f"{BLOCK_DEVICES}/sdb1")
        self.assertEqual(stick.icon, "drive-removable-media-usb")
        self.assertEqual(result["MOVIE"].kind, "optical")
        self.assertTrue(result["MOVIE"].can_eject)
        vault = result["Vault"]
        self.assertEqual((vault.kind, vault.mount_point, vault.backing_path, vault.can_unmount),
                         ("encrypted", "/data", f"{BLOCK_DEVICES}/nvme0n1p4", True))
        unmounted = result["32.0 GB volume"]
        self.assertEqual((unmounted.mounted, unmounted.can_mount, unmounted.can_unlock, unmounted.kind),
                         (False, True, False, "removable"))
        self.assertEqual(unmounted.space_text, "32.0 GB, not mounted")
        self.assertTrue(unmounted.can_eject)                 # no eject button, but it can be powered off
        locked = result["8.00 GB encrypted volume"]
        self.assertEqual((locked.can_unlock, locked.can_mount, locked.space_text), (True, False, "8.00 GB, locked"))
        self.assertEqual(result["share"].kind, "network")
        self.assertEqual(result["share"].object_path, "")
        order = [volume.name for volume in self.assemble({"blocks": blocks, "drives": drives})]
        self.assertEqual(order[0], "File system")
        self.assertEqual(order[-1], "share")

    def test_errors_in_plain_words(self):
        error = volumes.udisks_error
        self.assertEqual(error("org.freedesktop.UDisks2.Error.DeviceBusy", "target is busy"),
                         "The volume is in use. Close the files and folders on it and try again.")
        self.assertEqual(error("org.freedesktop.UDisks2.Error.NotAuthorizedDismissed", "x"),
                         "Authentication was cancelled.")
        self.assertEqual(error("org.freedesktop.UDisks2.Error.Failed", "Error unlocking /dev/sdd2: wrong passphrase"),
                         "That passphrase did not unlock the volume.")
        self.assertEqual(error("org.freedesktop.UDisks2.Error.Failed", "Something odd\nwith details"),
                         "Something odd")
        self.assertEqual(error("org.example.Strange", ""), "Strange")


if __name__ == "__main__":
    unittest.main()
