"""Pages of Control Center and the programs a page can start.

Pure data (no Qt, no I/O). Settings are native sections inside the window
(boswas_control_center/ui/settings: each section names the page it belongs
to); this module holds the pages themselves, their sidebar keywords, and the
few rows that start another Boswas program. A program maps to a name of
commands.EXECUTABLES and a fixed argument list; one that is not installed is
shown as unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Program:
    id: str                    # a key of PROGRAMS
    title: str
    description: str
    icon: str                  # icon name of the Boswas Prism theme
    optional: bool = False     # hidden (not just disabled) when not installed


# Program rows: key -> (commands.EXECUTABLES name, fixed arguments).
PROGRAMS = {
    "files": ("boswas-files", ("/",)),
    "compat-manager": ("boswas-compat-manager", ()),
}

FILES = Program("files", "Browse files", "Browse the whole file system in Files", "system-file-manager")
COMPAT_MANAGER = Program("compat-manager", "Compatibility Manager",
                         "Install, start and manage Windows applications", "boswas-compat-manager")


# --- pages ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PageInfo:
    key: str
    label: str                       # sidebar
    heading: str                     # page title
    description: str
    icons: tuple[str, ...]           # sidebar icon candidates, first existing wins
    programs: tuple[Program, ...] = ()
    programs_title: str = "Tools"
    keywords: tuple[str, ...] = ()
    live_only: bool = False


PAGES = (
    PageInfo("personalization", "Personalization", "Personalization",
             "Give your desktop a new look with a Boswas preset, or fine-tune each part of it.",
             ("preferences-desktop-theme-global", "preferences-desktop-theme", "preferences-desktop-color"),
             keywords=("theme", "preset", "colours", "colors", "wallpaper", "background", "dark", "light", "accent",
                       "fonts")),
    PageInfo("network", "Network", "Network",
             "Wi-Fi, wired connections, airplane mode, and the addresses this device uses.",
             ("preferences-system-network", "network-wired", "network-workgroup"),
             keywords=("wifi", "wi-fi", "wlan", "ethernet", "vpn", "proxy", "ip address", "internet", "airplane")),
    PageInfo("bluetooth", "Bluetooth", "Bluetooth", "Pair headphones, keyboards, phones and other Bluetooth devices.",
             ("preferences-system-bluetooth", "bluetooth"), keywords=("pair", "wireless", "headphones")),
    PageInfo("display", "Display", "Display",
             "Arrange your screens, set resolution and scaling, and reduce blue light in the evening.",
             ("preferences-desktop-display", "video-display"),
             keywords=("monitor", "screen", "resolution", "scale", "night light")),
    PageInfo("sound", "Sound", "Sound", "Choose speakers, headphones and microphones, and set volume levels.",
             ("preferences-desktop-sound", "audio-volume-high"),
             keywords=("audio", "volume", "speakers", "microphone", "mute")),
    PageInfo("power", "Power", "Power",
             "Battery status, the power mode, and when the screen turns off and the device locks.",
             ("preferences-system-power-management", "battery"),
             keywords=("battery", "energy", "sleep", "suspend", "brightness", "power mode")),
    PageInfo("storage", "Storage", "Storage",
             "Disks, file systems and how much space is used, and safe ways to free space.",
             ("boswas-storage", "drive-harddisk", "media-floppy"), (FILES,), "Tools",
             ("disk", "space", "files", "cleanup", "partition", "usb drive")),
    PageInfo("applications", "Applications", "Boswas Software Center",
             "Your applications in one place: open them, and install Linux software, Flatpak applications and "
             "AppImages.",
             ("boswas-software-center", "applications-other", "view-app-grid"),
             keywords=("apps", "software", "programs", "install", "remove", "deb", "debian", "flatpak", "flathub",
                       "appimage", "default applications", "start-up", "autostart")),
    PageInfo("windows", "Windows Compatibility", "Windows Compatibility",
             "The environment that runs Windows applications on Boswas OS, each in its own sandbox.",
             ("boswas-compat-manager", "application-x-ms-dos-executable", "wine"), (COMPAT_MANAGER,),
             "Manage", ("wine", "exe", "msi", "32-bit", "64-bit")),
    PageInfo("android", "Android", "Android Applications",
             "Run Android apps in Waydroid, an optional Android runtime with its own Android system.",
             ("boswas-android", "android", "phone", "smartphone"),
             keywords=("android", "apk", "waydroid", "f-droid", "fdroid", "google play", "play store", "mobile")),
    PageInfo("security", "Security", "Boswas Security Center",
             "How this device is protected. The checks run locally on this device.",
             ("boswas-security-center", "security-high", "preferences-security-firewall"),
             keywords=("firewall", "apparmor", "encryption", "secure boot", "screen lock")),
    PageInfo("users", "Users", "Users", "Your account, its password and picture, and the other people who use this "
             "device.",
             ("preferences-system-users", "system-users"), keywords=("account", "password", "picture", "user")),
    PageInfo("updates", "Updates", "Boswas Update Center",
             "How Boswas OS keeps this device up to date. Check for updates and install them.",
             ("boswas-update-center", "system-software-update", "update-none"),
             keywords=("upgrade", "security updates", "patch", "check for updates")),
    PageInfo("devices", "Devices", "Devices", "Mouse, touchpad and keyboard settings.",
             ("preferences-desktop-peripherals", "input-mouse", "input-keyboard"),
             keywords=("mouse", "keyboard", "touchpad", "pointer", "layout")),
    PageInfo("privacy", "Privacy", "Privacy",
             "What this device shares with your organisation, and settings that protect your activity.",
             ("preferences-system-privacy", "security-medium"),
             keywords=("telemetry", "data", "inventory", "history", "recent files")),
    PageInfo("hardware", "Hardware & Drivers", "Hardware & Drivers",
             "The devices in this computer, the driver each one uses, and firmware that is missing.",
             ("preferences-devices-tree", "hwinfo", "cpu", "computer"),
             keywords=("driver", "firmware", "pci", "usb", "device", "graphics", "wifi card", "ethernet")),
    PageInfo("system", "System", "System", "Date and time, region and language, and device management.",
             ("preferences-system", "computer"),
             keywords=("language", "time", "date", "time zone", "region", "locale", "device")),
    PageInfo("about", "About Boswas OS", "About Boswas OS",
             "System overview: version, hardware, current load and the state of the application runtimes.",
             ("boswas-logo", "help-about", "dialog-information"),
             keywords=("version", "hardware", "licence", "overview", "memory", "cpu", "build")),
    PageInfo("install", "Install Boswas OS", "Install Boswas OS",
             "You are using Boswas OS as a live session from the boot medium.",
             ("boswas-install", "system-software-install", "drive-harddisk"), live_only=True, keywords=("installer",)),
)
PAGE_KEYS = tuple(page.key for page in PAGES)
BY_KEY = {page.key: page for page in PAGES}

PROGRAM_IDS = frozenset(p.id for page in PAGES for p in page.programs)
