"""Pages of Control Center and the KDE modules and programs each one offers.

Pure data (no Qt, no I/O). Module IDs are the plugin names of KDE's
settings modules as installed by Debian trixie / Plasma 6.3
(…/qt6/plugins/plasma/kcms/{systemsettings,systemsettings_qwidgets}/);
kcmshell6 opens exactly one of them. Programs map to a fixed argument list.
A module or program that is not installed is shown as unavailable, or
hidden when it is marked optional.
"""

from __future__ import annotations

from dataclasses import dataclass

KCM, PROGRAM = "kcm", "program"


@dataclass(frozen=True)
class Module:
    id: str                    # KCM plugin name, or a key of PROGRAMS
    title: str
    description: str
    icon: str                  # icon name of the KDE icon theme
    kind: str = KCM
    optional: bool = False     # hidden (not just disabled) when not installed


# Program modules: key -> (commands.EXECUTABLES name, fixed arguments).
PROGRAMS = {
    "dolphin": ("dolphin", ("/",)),
    "partitionmanager": ("partitionmanager", ()),
    "compat-manager": ("boswas-compat-manager", ()),
    "kinfocenter": ("kinfocenter", ()),
}


def kcm(module_id: str, title: str, description: str, icon: str, optional: bool = False) -> Module:
    return Module(module_id, title, description, icon, KCM, optional)


def program(key: str, title: str, description: str, icon: str, optional: bool = False) -> Module:
    return Module(key, title, description, icon, PROGRAM, optional)


# --- modules -------------------------------------------------------------------------------------

LOOK_AND_FEEL = kcm("kcm_lookandfeel", "Global Theme", "Complete looks for Plasma, including Boswas",
                    "preferences-desktop-theme-global")
COLORS = kcm("kcm_colors", "Colours", "Colour schemes and the accent colour", "preferences-desktop-color")
WALLPAPER = kcm("kcm_wallpaper", "Wallpaper", "The desktop background", "preferences-desktop-wallpaper")
ICONS = kcm("kcm_icons", "Icons", "Icon theme and icon sizes", "preferences-desktop-icons")
FONTS = kcm("kcm_fonts", "Fonts", "Fonts and font sizes", "preferences-desktop-font")
SPLASH = kcm("kcm_splashscreen", "Splash Screen", "What is shown while Plasma starts", "preferences-system-splash")
NETWORK = kcm("kcm_networkmanagement", "Wi-Fi & Networking", "Wi-Fi, wired, mobile and VPN connections",
              "preferences-system-network")
PROXY = kcm("kcm_proxy", "Proxy", "Proxy server for network connections",
            "preferences-system-network-proxy")
BLUETOOTH = kcm("kcm_bluetooth", "Bluetooth", "Pair, connect and remove Bluetooth devices",
                "preferences-system-bluetooth")
DISPLAY = kcm("kcm_kscreen", "Display Configuration", "Arrangement, resolution and scaling",
              "preferences-desktop-display-randr")
NIGHT_LIGHT = kcm("kcm_nightlight", "Night Light", "Warmer colours in the evening", "redshift-status-on")
SOUND = kcm("kcm_pulseaudio", "Sound", "Output and input devices and volume levels", "preferences-desktop-sound")
POWER = kcm("kcm_powerdevilprofilesconfig", "Power Management",
            "Dimming, sleep, lid and power button", "preferences-system-power-management")
FIREWALL = kcm("kcm_firewall", "Firewall", "Firewall rules (needs administrator rights)",
               "preferences-security-firewall")
SCREEN_LOCK = kcm("kcm_screenlocker", "Screen Locking", "When the screen locks, and how it looks",
                  "preferences-desktop-user-password")
USERS = kcm("kcm_users", "Users", "Accounts, passwords and profile pictures", "preferences-system-users")
MOUSE = kcm("kcm_mouse", "Mouse", "Pointer speed, acceleration and button order", "preferences-desktop-mouse")
KEYBOARD = kcm("kcm_keyboard", "Keyboard", "Layouts, repeat rate and special keys", "preferences-desktop-keyboard")
TOUCHPAD = kcm("kcm_touchpad", "Touchpad", "Tapping, scrolling and gestures", "preferences-desktop-touchpad")
TABLET = kcm("kcm_tablet", "Drawing Tablet", "Pen and tablet mapping", "preferences-desktop-tablet")
KWALLET = kcm("kcm_kwallet5", "KDE Wallet", "Where applications store your passwords", "kwalletmanager",
              optional=True)
RECENT_FILES = kcm("kcm_recentFiles", "Recent Files", "Whether recently used files are remembered",
                   "document-open-recent")
FEEDBACK = kcm("kcm_feedback", "KDE User Feedback", "What Plasma itself may report to KDE",
               "preferences-desktop-feedback", optional=True)
REGION = kcm("kcm_regionandlang", "Region & Language", "Language, number, date and currency formats",
             "preferences-desktop-locale")
CLOCK = kcm("kcm_clock", "Date & Time", "Time zone and automatic time synchronisation", "preferences-system-time")
NOTIFICATIONS = kcm("kcm_notifications", "Notifications", "Which notifications appear, and Do Not Disturb",
                    "preferences-desktop-notification-bell")
DEFAULT_APPS = kcm("kcm_componentchooser", "Default Applications",
                   "Web browser, e-mail, file manager and more", "preferences-desktop-default-applications")
AUTOSTART = kcm("kcm_autostart", "Autostart", "Programs that start when you log in", "system-run")

FILES = program("dolphin", "Browse Files", "Browse the whole file system in Dolphin",
                "system-file-manager")
PARTITIONS = program("partitionmanager", "KDE Partition Manager", "Disks and partitions (administrator rights)",
                     "partitionmanager", optional=True)
COMPAT_MANAGER = program("compat-manager", "Compatibility Manager",
                         "Install, start and manage Windows applications", "boswas-compat-manager")
INFO_CENTER = program("kinfocenter", "System Information", "Detailed hardware and software information",
                      "hwinfo")


# --- pages ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PageInfo:
    key: str
    label: str                       # sidebar
    heading: str                     # page title
    description: str
    icons: tuple[str, ...]           # sidebar icon candidates, first existing wins
    modules: tuple[Module, ...] = ()
    modules_title: str = "Settings"
    keywords: tuple[str, ...] = ()
    live_only: bool = False


PAGES = (
    PageInfo("personalization", "Personalization", "Personalization",
             "Give your desktop a new look with a Boswas preset, or fine-tune each part of it.",
             ("preferences-desktop-theme-global", "preferences-desktop-theme", "preferences-desktop-color"),
             (LOOK_AND_FEEL, COLORS, WALLPAPER, ICONS, FONTS, SPLASH), "Customise further",
             ("theme", "preset", "colours", "wallpaper", "dark", "light")),
    PageInfo("network", "Network", "Network",
             "Connect to Wi-Fi, wired, mobile and VPN networks, and set a proxy.",
             ("preferences-system-network", "network-wired", "network-workgroup"),
             (NETWORK, PROXY), keywords=("wifi", "wlan", "ethernet", "vpn", "proxy")),
    PageInfo("bluetooth", "Bluetooth", "Bluetooth", "Pair headphones, keyboards, phones and other Bluetooth devices.",
             ("preferences-system-bluetooth", "bluetooth"), (BLUETOOTH,), keywords=("pair", "wireless")),
    PageInfo("display", "Display", "Display",
             "Arrange your screens, set resolution and scaling, and reduce blue light in the evening.",
             ("preferences-desktop-display", "video-display"), (DISPLAY, NIGHT_LIGHT),
             keywords=("monitor", "screen", "resolution", "scale")),
    PageInfo("sound", "Sound", "Sound", "Choose speakers, headphones and microphones, and set volume levels.",
             ("preferences-desktop-sound", "audio-volume-high"), (SOUND,), keywords=("audio", "volume")),
    PageInfo("power", "Power", "Power",
             "Battery status, energy saving, and what happens when you close the lid or press the power button.",
             ("preferences-system-power-management", "battery"), (POWER,),
             keywords=("battery", "energy", "sleep", "suspend")),
    PageInfo("storage", "Storage", "Storage", "How much space is used on this device.",
             ("boswas-storage", "drive-harddisk", "media-floppy"), (FILES, PARTITIONS), "Tools", ("disk", "space", "files")),
    PageInfo("applications", "Applications", "Boswas Software Center",
             "The applications installed on this device, in one place. Search for an application and open it.",
             ("boswas-software-center", "applications-other", "view-app-grid"), (DEFAULT_APPS, AUTOSTART),
             keywords=("apps", "software", "programs", "default", "autostart")),
    PageInfo("windows", "Windows Compatibility", "Windows Compatibility",
             "The environment that runs Windows applications on Boswas OS, each in its own sandbox.",
             ("boswas-compat-manager", "application-x-ms-dos-executable", "wine"), (COMPAT_MANAGER,),
             "Manage", ("wine", "exe", "msi")),
    PageInfo("security", "Security", "Boswas Security Center",
             "How this device is protected. The checks run locally on this device.",
             ("boswas-security-center", "security-high", "preferences-security-firewall"),
             (FIREWALL, SCREEN_LOCK), keywords=("firewall", "apparmor", "encryption", "secure boot")),
    PageInfo("users", "Users", "Users", "Manage user accounts, passwords and profile pictures.",
             ("preferences-system-users", "system-users"), (USERS,), keywords=("account", "password")),
    PageInfo("updates", "Updates", "Boswas Update Center", "How Boswas OS keeps this device up to date.",
             ("boswas-update-center", "system-software-update", "update-none"),
             keywords=("upgrade", "security updates", "patch")),
    PageInfo("devices", "Devices", "Devices", "Mouse, keyboard, touchpad and drawing tablet settings.",
             ("preferences-desktop-peripherals", "input-mouse", "input-keyboard"),
             (MOUSE, KEYBOARD, TOUCHPAD, TABLET), keywords=("mouse", "keyboard", "touchpad", "tablet")),
    PageInfo("privacy", "Privacy", "Privacy",
             "What this device shares with your organisation, and settings that protect your activity.",
             ("preferences-system-privacy", "security-medium"), (SCREEN_LOCK, RECENT_FILES, KWALLET, FEEDBACK),
             keywords=("telemetry", "data", "inventory", "history")),
    PageInfo("system", "System", "System", "Region, language, date, time, notifications and device management.",
             ("preferences-system", "computer"), (REGION, CLOCK, NOTIFICATIONS, INFO_CENTER),
             keywords=("language", "time", "date", "notifications", "device")),
    PageInfo("about", "About Boswas OS", "About Boswas OS", "Version and hardware information about this device.",
             ("boswas-logo", "help-about", "dialog-information"), keywords=("version", "hardware", "licence")),
    PageInfo("install", "Install Boswas OS", "Install Boswas OS",
             "You are using Boswas OS as a live session from the boot medium.",
             ("boswas-install", "system-software-install", "drive-harddisk"), live_only=True, keywords=("installer",)),
)
PAGE_KEYS = tuple(page.key for page in PAGES)
BY_KEY = {page.key: page for page in PAGES}

KCM_MODULES = frozenset(m.id for page in PAGES for m in page.modules if m.kind == KCM)
PROGRAM_MODULES = frozenset(m.id for page in PAGES for m in page.modules if m.kind == PROGRAM)

# Where kcmshell6 finds the modules (Debian amd64 and the generic Qt location).
KCM_PLUGIN_DIRS = ("/usr/lib/x86_64-linux-gnu/qt6/plugins/plasma/kcms", "/usr/lib/qt6/plugins/plasma/kcms")
KCM_PLUGIN_SUBDIRS = ("systemsettings", "systemsettings_qwidgets", "kinfocenter", "")
