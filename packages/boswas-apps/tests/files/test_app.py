"""The application around the windows: volumes through UDisks2 (a fake bus), arguments, FileManager1, large folders."""

from __future__ import annotations

import io
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ui_support import FilesCase, testing

from PySide6.QtCore import QObject, Qt, Signal


from boswas_apps.files import __main__ as main_module
from boswas_apps.files import app as app_module
from boswas_apps.files import places, volumes
from boswas_apps.files.app import FileManagerService, FilesApp
from boswas_apps.files.dialogs import PassphraseDialog
from boswas_apps.files.volumes import BLOCK, BLOCK_DEVICES, DRIVE, ENCRYPTED, FILESYSTEM

DRIVES = "/org/freedesktop/UDisks2/drives"


class FakeUDisks(QObject):
    """A UDisks2 with a mounted stick, an unmounted stick and a locked volume; calls are answered at once."""

    changed = Signal()

    def __init__(self, case: FilesCase) -> None:
        super().__init__()
        self.case = case
        self.calls: list[tuple[str, str, str, tuple]] = []
        self.fail: dict[str, tuple[str, str]] = {}
        self.watched = False
        self.blocks = {
            f"{BLOCK_DEVICES}/sdb1": {"IdUsage": "filesystem", "HintIgnore": False, "Device": b"/dev/sdb1\0",
                                      "IdLabel": "STICK", "IdType": "vfat", "Size": 16 * 2 ** 30,
                                      "Drive": f"{DRIVES}/stick", "CryptoBackingDevice": "/"},
            f"{BLOCK_DEVICES}/sdc1": {"IdUsage": "filesystem", "HintIgnore": False, "Device": b"/dev/sdc1\0",
                                      "IdLabel": "PHOTOS", "IdType": "ext4", "Size": 32 * 2 ** 30,
                                      "Drive": f"{DRIVES}/card", "CryptoBackingDevice": "/"},
            f"{BLOCK_DEVICES}/sdd1": {"IdUsage": "crypto", "HintIgnore": False, "Device": b"/dev/sdd1\0",
                                      "IdLabel": "", "IdType": "crypto_LUKS", "Size": 8 * 2 ** 30,
                                      "Drive": f"{DRIVES}/card", "CryptoBackingDevice": "/"},
        }
        self.drives = {
            f"{DRIVES}/stick": {"Removable": True, "MediaRemovable": True, "Ejectable": True, "Optical": False,
                                "ConnectionBus": "usb", "Vendor": "ACME", "Model": "Stick", "CanPowerOff": True},
            f"{DRIVES}/card": {"Removable": True, "MediaRemovable": True, "Ejectable": False, "Optical": False,
                               "ConnectionBus": "usb", "Vendor": "", "Model": "Reader", "CanPowerOff": False},
        }
        self.devices = {"/dev/sdb1": (8, 17), "/dev/sdc1": (8, 33), "/dev/sdd1": (8, 49), "/dev/dm-0": (253, 0)}
        self.mounted = {(8, 17): case.mkdir("home/media/STICK")}
        self.root = case.path("home")

    def watch(self) -> None:
        self.watched = True

    def mountinfo(self) -> str:
        lines = ["28 1 259:2 / / rw - ext4 /dev/root rw"]
        for number, ((major, minor), point) in enumerate(self.mounted.items()):
            lines.append(f"{40 + number} 28 {major}:{minor} / {point} rw - vfat /dev/x{number} rw")
        return "\n".join(lines) + "\n"

    def build(self, _mountinfo, udisks, user_home):
        mounts = volumes.parse_mountinfo(self.mountinfo())
        return volumes.assemble(mounts, udisks, user_home=user_home, usage_of=lambda _p: (2 ** 34, 2 ** 33),
                                label_of={}, devnum_of=self.devices.get)

    def call(self, path, interface, method, arguments, callback, _timeout=0) -> None:
        self.calls.append((path, interface, method, tuple(str(a) if not isinstance(a, dict) else "{}"
                                                           for a in arguments)))
        if method in self.fail:
            callback(False, list(self.fail[method]))
        elif method == "Introspect":
            callback(True, ["".join(f'<node name="{name.rsplit("/", 1)[1]}"/>' for name in self.blocks)])
        elif method == "Get":
            source = self.blocks if arguments[0] == BLOCK else self.drives if arguments[0] == DRIVE else {}
            value = source.get(path, {}).get(arguments[1])
            callback(value is not None, [value] if value is not None else ["org.x.UnknownProperty", ""])
        elif (interface, method) == (FILESYSTEM, "Mount"):
            device = self.devices[self.blocks[path]["Device"].rstrip(b"\0").decode()]
            point = self.case.mkdir("home/media/" + (self.blocks[path]["IdLabel"] or "volume"))
            self.mounted[device] = point
            callback(True, [point])
        elif (interface, method) == (FILESYSTEM, "Unmount"):
            device = self.devices[self.blocks[path]["Device"].rstrip(b"\0").decode()]
            self.mounted.pop(device, None)
            callback(True, [])
        elif (interface, method) == (ENCRYPTED, "Unlock"):
            if arguments[0] != "correct horse":
                callback(False, ["org.freedesktop.UDisks2.Error.Failed", "Error unlocking: wrong passphrase"])
                return
            clear = f"{BLOCK_DEVICES}/dm_2d0"
            self.blocks[clear] = {"IdUsage": "filesystem", "HintIgnore": False, "Device": b"/dev/dm-0\0",
                                  "IdLabel": "Secrets", "IdType": "ext4", "Size": 8 * 2 ** 30, "Drive": "/",
                                  "CryptoBackingDevice": path}
            callback(True, [clear])
        elif (interface, method) == (ENCRYPTED, "Lock"):
            self.blocks.pop(f"{BLOCK_DEVICES}/dm_2d0", None)
            callback(True, [])
        else:
            callback(True, [])

    action = call

    def actions(self) -> list[tuple[str, str]]:
        return [(path.rsplit("/", 1)[1], method) for path, _interface, method, _args in self.calls
                if method not in ("Introspect", "Get")]


class VolumesTest(FilesCase):
    def make(self):
        bus = FakeUDisks(self)
        services = self.services(bus=bus)
        services.volumes._build = bus.build
        services.volumes.refresh()
        services.worker.wait(5)
        testing.process()
        window = self.window(places.COMPUTER, services)
        return bus, services, window

    def volume(self, services, name):
        return next(volume for volume in services.volumes.volumes if volume.name == name)

    def test_this_pc_lists_volumes_with_space(self):
        bus, services, window = self.make()
        tab = window.tab()
        self.assertIs(tab.page(), tab.volumes_page)
        self.assertTrue(services.volumes.has_udisks)
        names = [volume.name for volume in tab.volumes_page.volumes()]
        self.assertEqual(names, ["File system", "8.00 GB encrypted volume", "PHOTOS", "STICK"])
        self.assertFalse(tab.volumes_page.note.isVisible())
        stick = self.volume(services, "STICK")
        self.assertEqual(stick.space_text, "8.00 GB free of 16.0 GB")
        self.assertEqual(window.count_label.text(), "4 volumes")
        self.assertEqual(window.address.crumbs.bar.items(), ["This PC"])
        self.assertFalse(window.actions_["new_folder"].isEnabled())
        self.assertFalse(window.search_box.isEnabled())
        sidebar = [window.sidebar.item(row).text() for row in range(window.sidebar.count())]
        self.assertEqual(sidebar[sidebar.index("This PC"):], ["This PC", "File system", "8.00 GB encrypted volume",
                                                              "PHOTOS", "STICK", "", "Trash"])
        testing.grab(window, self.tmp / "this-pc.png")
        # opening a mounted volume shows it; the breadcrumb starts at the volume
        tab.volumes_page.activated.emit(stick)
        self.settle(window)
        self.assertEqual(tab.location, stick.mount_point)
        self.assertEqual(window.address.crumbs.bar.items(), ["Home", "media", "STICK"])
        properties = self.keep(window.show_properties(volume=stick))
        self.assertEqual(properties.value("Type"), "Removable drive")
        self.assertEqual(properties.value("File system"), "vfat")
        self.assertEqual(properties.value("Capacity"), "16.0 GB (17,179,869,184 bytes)")
        self.assertEqual(properties.usage_bar.value(), 500)
        properties.close()

    def test_mount_unmount_and_eject(self):
        bus, services, window = self.make()
        tab = window.tab()
        photos = self.volume(services, "PHOTOS")
        self.assertEqual(self.menu_texts(window._volume_menu(photos)), ["Mount", "Properties"])
        tab.volumes_page.activated.emit(photos)
        self.settle(window)
        self.assertEqual(bus.actions(), [("sdc1", "Mount")])
        self.assertEqual(tab.location, self.path("home/media/PHOTOS"))
        photos = self.volume(services, "PHOTOS")
        self.assertTrue(photos.mounted)
        self.assertEqual(self.menu_texts(window._volume_menu(photos)),
                         ["Open", "Open in new tab", "Open in new window", "Unmount", "Properties"])
        self.trigger(window._volume_menu(photos), "Unmount")
        self.settle(window)
        self.assertEqual(bus.actions()[-1], ("sdc1", "Unmount"))
        self.assertEqual(tab.location, places.COMPUTER)          # the tab left the volume first
        self.assertFalse(self.volume(services, "PHOTOS").mounted)
        self.assertIn("unmounted", window.banner.message())
        # the eject mark in the navigation pane: unmount, then power the stick off
        stick = self.volume(services, "STICK")
        item = next(window.sidebar.item(row) for row in range(window.sidebar.count())
                    if window.sidebar.item(row).text() == "STICK")
        rect = window.sidebar.itemDelegate().eject_rect(window.sidebar.visualItemRect(item))
        testing.click(window.sidebar.viewport(), rect.center())
        self.settle(window)
        self.assertEqual(bus.actions()[-2:], [("sdb1", "Unmount"), ("stick", "PowerOff")])
        self.assertIn("can be removed", window.banner.message())
        self.assertIsNotNone(stick)

    def test_errors_of_the_storage_service_are_told(self):
        bus, services, window = self.make()
        bus.fail["Mount"] = ("org.freedesktop.UDisks2.Error.NotAuthorizedDismissed", "dismissed")
        window.activate_volume(self.volume(services, "PHOTOS"))
        self.settle(window)
        self.assertEqual(window.banner.message(),
                         "“PHOTOS” could not be mounted: Authentication was cancelled.")
        self.assertEqual(window.tab().location, places.COMPUTER)
        bus.fail = {"Unmount": ("org.freedesktop.UDisks2.Error.DeviceBusy", "target is busy")}
        window.eject_volume(self.volume(services, "STICK"))
        self.settle(window)
        self.assertIn("is in use", window.banner.message())
        self.assertTrue(self.volume(services, "STICK").mounted)

    def test_unlock_asks_for_the_passphrase(self):
        bus, services, window = self.make()
        locked = self.volume(services, "8.00 GB encrypted volume")
        self.assertEqual(self.menu_texts(window._volume_menu(locked)), ["Unlock…", "Properties"])
        window.activate_volume(locked)
        dialog = window.open_dialogs()[0]
        self.assertIsInstance(dialog, PassphraseDialog)
        self.assertFalse(dialog.primary_button.isEnabled())
        testing.type_text(dialog.box, "wrong")
        testing.grab(window, self.tmp / "passphrase.png")
        testing.key(dialog.box, Qt.Key.Key_Return)
        self.settle(window)
        self.assertIn("That passphrase did not unlock the volume", window.banner.message())
        self.assertNotIn("wrong", str(bus.calls[-1][3][1:]))
        window.activate_volume(locked)
        dialog = window.open_dialogs()[0]
        dialog.box.setText("correct horse")
        testing.click(dialog.primary_button)
        self.settle(window)
        self.assertEqual(bus.actions()[-2:], [("sdd1", "Unlock"), ("dm_2d0", "Mount")])
        self.assertEqual(window.tab().location, self.path("home/media/Secrets"))
        secrets = self.volume(services, "Secrets")
        self.assertEqual((secrets.kind, secrets.backing_path), ("encrypted", f"{BLOCK_DEVICES}/sdd1"))
        window.eject_volume(secrets, eject=False)
        self.settle(window)
        self.assertEqual(bus.actions()[-2:], [("dm_2d0", "Unmount"), ("sdd1", "Lock")])

    def test_without_udisks_volumes_are_listed_read_only(self):
        services = self.services()
        services.volumes._build = lambda _m, udisks, _h: volumes.assemble(
            volumes.parse_mountinfo("28 1 259:2 / / rw - ext4 /dev/root rw\n41 28 8:17 / /media/x rw - vfat /dev/sdb1 rw\n"),
            udisks, usage_of=lambda _p: (100, 50), label_of={})
        services.volumes.refresh()
        services.worker.wait(5)
        window = self.window(places.COMPUTER, services)
        self.assertFalse(services.volumes.has_udisks)
        page = window.tab().volumes_page
        self.assertTrue(page.note.isVisible())
        self.assertEqual([volume.name for volume in page.volumes()], ["File system", "x"])
        for volume in page.volumes():
            self.assertEqual(self.menu_texts(window._volume_menu(volume)),
                             ["Open", "Open in new tab", "Open in new window", "Properties"])


class ApplicationTest(FilesCase):
    def files_app(self) -> FilesApp:
        files = FilesApp(self.services())
        self.addCleanup(lambda: [window.close() for window in list(files.windows)])
        return files

    def wait(self, files: FilesApp) -> None:
        for window in files.windows:
            self.settle(window)

    def test_first_start_and_later_starts(self):
        files = self.files_app()
        files.handle_arguments([], first=True)
        self.wait(files)
        self.assertEqual(len(files.windows), 1)
        window = files.windows[0]
        self.assertEqual(window.tab().location, self.home)
        files.handle_arguments([])                           # started again: the window is brought forward
        self.assertEqual(len(files.windows), 1)
        self.assertEqual(window.tabs.count(), 1)
        files.handle_arguments([self.docs])
        self.wait(files)
        self.assertEqual(len(files.windows), 1)
        self.assertEqual([tab.location for tab in window.all_tabs()], [self.home, self.docs])
        self.assertEqual(window.tab().location, self.docs)
        files.handle_arguments([self.docs])                  # already open: that tab is shown, no new one
        self.assertEqual(window.tabs.count(), 2)
        files.handle_arguments(["--new-window", "trash:///", "file://" + self.docs])
        self.wait(files)
        self.assertEqual(len(files.windows), 2)
        self.assertEqual([tab.location for tab in files.windows[1].all_tabs()], [places.TRASH, self.docs])
        files.windows[1].close()
        self.assertEqual(len(files.windows), 1)

    def test_select(self):
        target = self.write("home/Documents/deep/report.txt")
        other = self.write("home/Downloads/other.bin")
        files = self.files_app()
        files.handle_arguments(["--select", target], first=True)
        self.wait(files)
        window = files.windows[0]
        self.assertEqual(window.tab().location, os.path.dirname(target))
        self.assertEqual([entry.path for entry in window.tab().selected_entries()], [target])
        files.handle_arguments(["--select", other])
        self.wait(files)
        self.assertEqual(window.tabs.count(), 2)
        self.assertEqual([entry.path for entry in window.tab().selected_entries()], [other])
        files.handle_arguments([target])                     # a file as argument: its folder, the file selected
        self.wait(files)
        self.assertEqual(window.tab().location, os.path.dirname(target))
        self.assertEqual([entry.path for entry in window.tab().selected_entries()], [target])

    def test_hostile_arguments(self):
        files = self.files_app()
        files.handle_arguments(["--no-such-option", "\0", "relative/../..", "http://example.org/", "--select"],
                               first=True)
        self.wait(files)
        self.assertEqual(len(files.windows), 1)
        files.handle_arguments(["x" * 10000, "--select", "trash:///", "--bogus"] + ["/"] * 500)
        self.wait(files)
        self.assertLessEqual(files.windows[0].tabs.count(), 70)

    def test_file_manager_service(self):
        target = self.write("home/Documents/shown item.txt")
        files = self.files_app()
        service = FileManagerService(files)
        service.ShowItems(["file://" + target.replace(" ", "%20"), "http://example.org/x", "file://host/x"], "token")
        self.wait(files)
        self.assertEqual(files.activation_token, "token")
        window = files.windows[0]
        self.assertEqual(window.tab().location, self.docs)
        self.assertEqual([entry.name for entry in window.tab().selected_entries()], ["shown item.txt"])
        service.ShowFolders(["file://" + os.path.join(self.home, "Downloads"), "not a uri"], "")
        self.wait(files)
        self.assertEqual(window.tab().location, os.path.join(self.home, "Downloads"))
        self.assertEqual(window.tabs.count(), 2)
        second = self.write("home/Documents/second.txt")
        service.ShowItems(["file://" + second], "")           # the folder is open already: its tab is used
        self.wait(files)
        self.assertEqual(window.tabs.count(), 2)
        self.assertEqual([entry.name for entry in window.tab().selected_entries()], ["second.txt"])
        service.ShowItemProperties(["file://" + second], "")
        testing.process(20)
        self.assertEqual(len(window._properties), 1)
        self.assertEqual(window._properties[0].windowTitle(), "second.txt – Properties")
        window._properties[0].close()
        self.assertIsNone(app_module.register_file_manager(files))      # no session bus in the tests

    def test_arguments_for_the_running_instance_are_absolute(self):
        here = os.getcwd()
        self.assertEqual(app_module.absolute_arguments(["--new-window", "sub/dir", "/abs", "~/x", "file:///y",
                                                        "trash:///", "--select", "a.txt"]),
                         ["--new-window", os.path.join(here, "sub/dir"), "/abs", "~/x", "file:///y", "trash:///",
                          "--select", os.path.join(here, "a.txt")])

    def test_main_version_help_and_root(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main_module.main(["boswas-files", "--version"]), 0)
        self.assertRegex(out.getvalue(), r"^boswas-files \S+\n$")
        with redirect_stdout(io.StringIO()) as text, self.assertRaises(SystemExit) as stop:
            main_module.main(["boswas-files", "--help"])
        self.assertEqual(stop.exception.code, 0)
        self.assertIn("--select", text.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            main_module.main(["boswas-files", "--frobnicate"])
        self.assertEqual(stop.exception.code, 2)
        original = os.geteuid
        os.geteuid = lambda: 0
        try:
            with redirect_stderr(io.StringIO()) as text:
                self.assertEqual(main_module.main(["boswas-files"]), 4)
        finally:
            os.geteuid = original
        self.assertIn("never as root", text.getvalue())

    def test_closing_the_last_window_with_running_operations_asks(self):
        from boswas_apps.files.fileops import Operation

        class Endless(Operation):
            title = "Working"

            def _run(self):
                while True:
                    self._check()
                    time.sleep(0.005)

        files = self.files_app()
        files.handle_arguments([], first=True)
        self.wait(files)
        window = files.windows[0]
        answers = [False, True]
        window.ask = lambda *args, **kwargs: answers.pop(0)
        files.services.jobs.run(Endless(), owner=window)
        window.close()
        self.assertTrue(window.isVisible())
        self.assertTrue(files.services.jobs.busy())
        window.close()
        self.assertFalse(window.isVisible())
        self.assertTrue(files.services.jobs.wait(5))
        self.assertEqual(files.windows, [])


class LargeFolderTest(FilesCase):
    def test_a_hundred_thousand_entries(self):
        big = self.mkdir("home/big")
        for number in range(100_000):
            os.close(os.open(os.path.join(big, f"item{number}.dat"), os.O_CREAT | os.O_WRONLY, 0o644))
        window = self.window()
        tab = window.tab()
        longest = 0.0
        tab.navigate(big)
        last = time.monotonic()
        first_rows = None
        while tab.model.is_loading:
            testing.process()
            now = time.monotonic()
            longest = max(longest, now - last)
            last = now
            if first_rows is None and tab.model.rows:
                first_rows = len(tab.model.rows)
        self.settle(window)
        self.assertEqual(len(tab.model.rows), 100_000)
        self.assertLess(first_rows, 100_000)                   # rows were shown before the folder was read to its end
        self.assertLess(longest, 1.0, "the GUI thread was held for too long while the folder was read")
        self.assertEqual([entry.name for entry in tab.model.rows[:3]], ["item0.dat", "item1.dat", "item2.dat"])
        self.assertEqual(tab.model.rows[-1].name, "item99999.dat")
        self.assertEqual(window.count_label.text(), "100,000 items")
        # nothing is made per file: the views hold a handful of child widgets however long the folder is
        self.assertLess(len(tab.view.tree.viewport().children()), 20)
        started = time.monotonic()
        tab.view.select_all()
        testing.process()
        self.assertEqual(tab.view.selected_count(), 100_000)
        self.assertEqual(window.selection_label.text(), "100,000 items selected")
        tab.view.tree.scrollToBottom()
        testing.grab(window, self.tmp / "big.png")
        window.set_view_mode("icons-medium")
        testing.process(50)
        testing.grab(window, self.tmp / "big-icons.png")
        self.assertLess(time.monotonic() - started, 6.0)
        self.assertLess(len(tab.view.grid.viewport().children()), 20)
        window.set_view_mode("details")
        started = time.monotonic()
        tab.view.sort("name", True)                            # sorted on a worker thread, swapped in when done
        self.assertLess(time.monotonic() - started, 0.5)
        self.settle(window)
        self.assertEqual(tab.model.rows[0].name, "item99999.dat")
        tab.view.select_none()
        testing.type_text(tab.view.tree, "item5")
        self.assertEqual(tab.selected_entries()[0].name, "item59999.dat")


if __name__ == "__main__":
    unittest.main()
