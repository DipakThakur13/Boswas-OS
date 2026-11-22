"""Privacy & security > App permissions: what sandboxed applications were allowed (no Qt).

Flatpak applications ask the desktop portals (xdg-desktop-portal) for the
camera, the location, a screenshot, running in the background, ...; the
answers are kept in the user's permission store, which `flatpak
permission-list` prints, one permission per line, separated by tabs:

    TABLE  OBJECT  APP  PERMISSIONS  DATA

Taking a permission back is `flatpak permission-remove TABLE OBJECT APP`
(commands.permission_remove_argv): the decision is forgotten and the
application has to ask again. Applications that are not sandboxed (Debian
packages, AppImages, the Boswas applications) never ask: they have the
permissions of the user's account and are not in the store.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import commands
from . import viewmodel as vm

NOT_SANDBOXED = ("Applications installed as Debian packages, AppImages and the Boswas applications are not "
                 "sandboxed: they run with the permissions of your account, can use the camera, the microphone and "
                 "your files directly, and never appear here. Install software you do not fully trust as a Flatpak "
                 "application.")
NAMES = {
    ("devices", "camera"): ("Camera", "camera-web"),
    ("devices", "microphone"): ("Microphone", "audio-input-microphone-symbolic"),
    ("devices", "speakers"): ("Speakers", "audio-volume-high-symbolic"),
    ("location", "location"): ("Location", "mark-location-symbolic"),
    ("screenshot", "screenshot"): ("Screenshots", "screenshot-symbolic"),
    ("background", "background"): ("Run in the background", "system-run-symbolic"),
    ("notifications", "notification"): ("Notifications", "notification-symbolic"),
    ("wallpaper", "wallpaper"): ("Change the background", "preferences-desktop-wallpaper"),
    ("inhibit", "inhibit"): ("Keep the device awake", "system-suspend-symbolic"),
    ("realtime", "realtime"): ("Real-time priority", "utilities-system-monitor-symbolic"),
}
LOCATION_LEVELS = {"EXACT": "exact", "STREET": "street", "NEIGHBORHOOD": "neighbourhood", "CITY": "city",
                   "COUNTRY": "country", "NONE": "none"}


@dataclass(frozen=True)
class Permission:
    table: str
    ident: str
    app: str
    value: str               # Allowed, Denied, Ask every time, or what the store holds

    @property
    def title(self) -> str:
        return NAMES.get((self.table, self.ident), (f"{self.table}: {self.ident}",))[0]

    @property
    def icon(self) -> str:
        return NAMES.get((self.table, self.ident), ("", "dialog-question-symbolic"))[1]


def _value(words: list[str]) -> str:
    first = words[0].strip() if words else ""
    if first == "yes":
        return "Allowed"
    if first == "no":
        return "Denied"
    if first == "ask":
        return "Ask every time"
    if first in LOCATION_LEVELS:
        level = LOCATION_LEVELS[first]
        return "Denied" if level == "none" else f"Allowed ({level} accuracy)"
    return vm.one_line(first, 40) or "Unknown"


def parse_list(text: str) -> list[Permission]:
    """The permissions of applications in `flatpak permission-list` (tab-separated, the title line skipped)."""
    found = []
    for line in (text or "").splitlines()[:5000]:
        parts = line.split("\t")
        if len(parts) < 4 or parts[0].strip() == "Table":
            continue
        table, ident, app, values = (part.strip() for part in parts[:4])
        if not (commands.valid_permission_name(table) and commands.valid_permission_name(ident)
                and commands.FLATPAK_APP_RE.fullmatch(app or "")):
            continue
        found.append(Permission(table, ident, app, _value(values.split(","))))
    return found


def by_app(permissions: list[Permission]) -> dict[str, list[Permission]]:
    groups: dict[str, list[Permission]] = {}
    for permission in permissions:
        groups.setdefault(permission.app, []).append(permission)
    for items in groups.values():
        items.sort(key=lambda p: p.title)
    return dict(sorted(groups.items(), key=lambda item: item[0].casefold()))
