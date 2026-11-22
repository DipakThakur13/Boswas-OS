"""Apps > Installed apps: every application on the device, where it comes from and how it is removed (no Qt).

The list is made of what the Software Center already reads:

  desktop entries        probes.desktop_entries (what the application menu shows)
  dpkg-query -S PATH...  the Debian package that owns an entry (read-only)
  dpkg-query -W ...      the package's version and installed size
  flatpak list           the Flatpak applications of the user and of the system
  ~/Applications         the AppImages added in the Software Center (userfiles)
  the session service    the Windows applications (apps.list)

An application is removed the way it was installed: a Debian package through
the system helper (sysops apt-remove: administrator password, never a part of
Boswas OS), a Flatpak application of the user with `flatpak uninstall --user`,
an AppImage by deleting its file and menu entry (userfiles), a Windows
application in the Compatibility Manager. Flatpak applications installed for
every user, packages of Boswas OS and entries of unknown origin are listed
without Uninstall, with the reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import commands
from . import viewmodel as vm

DEBIAN, FLATPAK, APPIMAGE, WINDOWS, OTHER = "debian", "flatpak", "appimage", "windows", "other"
SOURCE_LABELS = {DEBIAN: "Debian package", FLATPAK: "Flatpak", APPIMAGE: "AppImage", WINDOWS: "Windows application",
                 OTHER: "Other"}
FILTERS = (("all", "All apps"), (DEBIAN, "Debian packages"), (FLATPAK, "Flatpak"), (APPIMAGE, "AppImages"),
           (WINDOWS, "Windows applications"))
SORTS = (("name", "Name (A to Z)"), ("name-desc", "Name (Z to A)"), ("size", "Size (largest first)"))
FLATPAK_EXPORTS = "/flatpak/exports/share/applications/"
APPIMAGE_PREFIX = "boswas-appimage-"


@dataclass(frozen=True)
class App:
    key: str
    name: str
    source: str
    icon: str = ""
    package: str = ""        # Debian
    version: str = ""
    size: int | None = None  # bytes, where known
    flatpak_id: str = ""
    installation: str = ""   # Flatpak: user or system
    appimage_id: str = ""
    windows_id: str = ""
    removable: bool = False
    why_not: str = ""        # why there is no Uninstall

    @property
    def detail(self) -> str:
        parts = [SOURCE_LABELS[self.source]]
        if self.package:
            parts.append(self.package)
        elif self.flatpak_id:
            parts.append(self.flatpak_id + (" · for all users" if self.installation == "system" else ""))
        if self.version:
            parts.append(self.version)
        if self.size:
            parts.append(vm.format_bytes(self.size))
        return " · ".join(parts)


def parse_owners(text: str) -> dict[str, str]:
    """{path: package} from `dpkg-query -S PATH...` ("pkg[:arch][, pkg2]: /path"; diversions are skipped)."""
    owners = {}
    for line in (text or "").splitlines():
        if line.startswith(("diversion by", "local diversion")) or ": " not in line:
            continue
        packages, _, path = line.partition(": ")
        first = packages.split(",")[0].strip().split(":")[0]
        if commands.valid_package(first) and path.startswith("/"):
            owners[path.strip()] = first
    return owners


def debian_paths(entries, home: str | None = None) -> list[str]:
    """The entries whose owner dpkg may know: not in a home folder, not under /usr/local, not exported by Flatpak."""
    return [e.path for e in entries if not e.winapp_id and FLATPAK_EXPORTS not in e.path
            and not e.path.startswith("/usr/local/") and not (home and e.path.startswith(home + os.sep))]


def assemble(entries, owners: dict[str, str], status: dict, flatpak_apps, appimages, windows_apps,
             home: str | None) -> list[App]:
    """One row per application. ``status``: features.parse_status of the owning packages."""
    found: dict[str, App] = {}
    user_flatpaks = {a.app_id for a in flatpak_apps if a.installation == "user"}
    images = {a.ident: a for a in appimages}
    for entry in entries:
        if entry.winapp_id:
            continue                          # listed from the session service below
        path = entry.path
        if FLATPAK_EXPORTS in path:
            installation = "user" if home and path.startswith(home + os.sep) else "system"
            if entry.desktop_id in user_flatpaks:
                installation = "user"
            app = App(f"flatpak:{entry.desktop_id}", entry.name, FLATPAK, entry.icon, flatpak_id=entry.desktop_id,
                      installation=installation, removable=installation == "user",
                      why_not="" if installation == "user" else "Installed for all users by your administrator.")
        elif entry.desktop_id.startswith(APPIMAGE_PREFIX) and entry.desktop_id[len(APPIMAGE_PREFIX):] in images:
            ident = entry.desktop_id[len(APPIMAGE_PREFIX):]
            image = images[ident]
            app = App(f"appimage:{ident}", entry.name, APPIMAGE, entry.icon, size=image.size or None,
                      appimage_id=ident, removable=True)
        elif path in owners:
            package = owners[path]
            state = status.get(package, ("", "", None))
            ours = package.startswith("boswas-")
            app = App(f"debian:{entry.desktop_id}", entry.name, DEBIAN, entry.icon, package=package,
                      version=state[1], size=state[2], removable=not ours,
                      why_not="A part of Boswas OS." if ours else "")
        else:
            where = "Added in your home folder." if home and path.startswith(home + os.sep) else \
                "Installed outside the package manager."
            app = App(f"other:{entry.desktop_id}", entry.name, OTHER, entry.icon, why_not=where)
        found.setdefault(app.key, app)
    for item in windows_apps or []:
        if not isinstance(item, dict):
            continue
        app_id = vm.one_line(item.get("id"), 96)
        if not app_id:
            continue
        found[f"windows:{app_id}"] = App(f"windows:{app_id}", vm.one_line(item.get("name"), 120) or app_id, WINDOWS,
                                         "application-x-ms-dos-executable", version=vm.one_line(item.get("version"),
                                                                                                40),
                                         windows_id=app_id, removable=True)
    return list(found.values())


def view(apps: list[App], query: str = "", source: str = "all", order: str = "name") -> list[App]:
    words = query.casefold().split()
    shown = [a for a in apps if (source == "all" or a.source == source)
             and all(word in f"{a.name} {a.package} {a.flatpak_id}".casefold() for word in words)]
    if order == "size":
        shown.sort(key=lambda a: (-(a.size or 0), a.name.casefold()))
    else:
        shown.sort(key=lambda a: a.name.casefold(), reverse=order == "name-desc")
    return shown
