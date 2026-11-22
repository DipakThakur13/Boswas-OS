"""The places of Control Center: categories, their pages, the programs a page can start, and what search finds.

Pure data and pure functions (no Qt, no I/O). The window is laid out like
the settings of a modern desktop: the left pane holds the categories (Home,
System, Bluetooth & devices, Network & internet, ...); a category page lists
its pages as cards, and a page holds the settings themselves. Settings are
native sections inside the window (boswas_control_center/ui/settings: each
section names the page it belongs to); this module holds the places, their
keywords, the few rows that start another Boswas program, and the deep links
of earlier versions.

Every place has a key that ``boswas-control-center --page KEY`` accepts. A
category is a place without a parent; a page has the key of its category as
``parent``. A program maps to a name of commands.EXECUTABLES and a fixed
argument list; one that is not installed is shown as unavailable.

Adding a page later (Taskbar, Notifications, Text size, ...) is one entry in
PAGES with the category as parent, and its sections in a settings module.
"""

from __future__ import annotations

import re
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
    "camera": ("boswas-camera", ()),
}

FILES = Program("files", "Browse files", "Browse the whole file system in Files", "system-file-manager")
COMPAT_MANAGER = Program("compat-manager", "Compatibility Manager",
                         "Install, start and manage Windows applications", "boswas-compat-manager")
CAMERA = Program("camera", "Open Camera", "Take photos and videos with the Camera application", "camera-photo")


# --- places -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PageInfo:
    key: str
    label: str                       # pane item (categories) or card title (pages)
    heading: str                     # page title (the last crumb of the path above the page)
    description: str                 # the line at the top of the page
    icons: tuple[str, ...]           # icon candidates, first existing wins
    programs: tuple[Program, ...] = ()
    programs_title: str = "Tools"
    keywords: tuple[str, ...] = ()
    live_only: bool = False
    parent: str = ""                 # the category of a page; "" for a category
    summary: str = ""                # the one line on the page's card in its category (default: description)

    @property
    def is_category(self) -> bool:
        return not self.parent

    @property
    def card_text(self) -> str:
        return self.summary or self.description


def _category(key, label, description, icons, keywords=(), heading=None, programs=(), programs_title="Tools"):
    return PageInfo(key, label, heading or label, description, icons, programs, programs_title, tuple(keywords))


def _page(parent, key, label, summary, icons, keywords=(), *, heading=None, description=None, programs=(),
          programs_title="Tools", live_only=False):
    return PageInfo(key, label, heading or label, description or summary, icons, programs, programs_title,
                    tuple(keywords), live_only, parent, summary)


PAGES = (
    # --- Home ---
    _category("home", "Home", "Your device at a glance, and the settings you use most.",
              ("go-home-symbolic", "user-home", "preferences-system"),
              ("overview", "start", "recommended", "device name", "summary")),

    # --- System ---
    _category("system", "System", "Display, sound, power, storage, and help when something does not work.",
              ("computer", "preferences-system"), ("pc", "computer", "settings", "device")),
    _page("system", "display", "Display", "Monitors, brightness, night light, resolution and scale",
          ("preferences-desktop-display", "video-display"),
          ("monitor", "screen", "resolution", "scale", "night light", "refresh rate"),
          description="Arrange your screens, set resolution and scaling, and reduce blue light in the evening."),
    _page("system", "sound", "Sound", "Volume, output and input devices",
          ("preferences-desktop-sound", "audio-volume-high-symbolic"),
          ("audio", "volume", "speakers", "microphone", "mute"),
          description="Choose speakers, headphones and microphones, and set volume levels."),
    _page("system", "notifications", "Notifications", "Do not disturb, notifications from apps",
          ("preferences-desktop-notification-bell", "notification-symbolic"),
          ("notifications", "do not disturb", "dnd", "quiet hours", "banners", "toasts", "alerts"),
          description="When notifications pop up, and which applications may show them."),
    _page("system", "power", "Power & battery", "Battery, power mode, brightness, screen time-out",
          ("preferences-system-power-management", "battery-full-symbolic"),
          ("battery", "energy", "sleep", "suspend", "brightness", "power mode"),
          description="Battery status, the power mode, and when the screen turns off."),
    _page("system", "storage", "Storage", "Storage space, drives, cleaning up files",
          ("boswas-storage", "drive-harddisk"),
          ("disk", "space", "files", "cleanup", "clean up", "partition", "usb drive", "trash", "temporary files",
           "downloads", "cache"),
          description="Disks, file systems and how much space is used, and safe ways to free space.",
          programs=(FILES,)),
    _page("system", "multitasking", "Multitasking", "Snap windows, desktops, Alt+Tab",
          ("preferences-system-windows", "view-dual-symbolic"),
          ("snap", "snap layouts", "split screen", "virtual desktops", "alt+tab", "task switcher", "windows"),
          description="How windows are arranged side by side, and which windows the taskbar and Alt+Tab show."),
    _page("system", "troubleshoot", "Troubleshoot", "Checks for the internet connection, sound, Bluetooth and display",
          ("boswas-repair", "tools-report-bug"),
          ("troubleshooter", "fix", "problem", "not working", "diagnose", "help", "no internet", "no sound"),
          description="Each check looks at the device and says in plain words what it found and what helps. "
                      "Nothing is changed."),
    _page("system", "recovery", "Recovery", "Restart into the firmware settings or the boot menu",
          ("system-reboot-symbolic", "boswas-repair"),
          ("uefi", "bios", "firmware", "advanced startup", "boot menu", "restart", "recovery"),
          description="Advanced start-up: restart this device into its firmware settings or its boot menu."),
    _page("system", "about", "About", "Device specifications, rename this device, Boswas OS version",
          ("help-about-symbolic", "boswas-logo"),
          ("version", "hardware", "licence", "overview", "memory", "cpu", "build", "device name", "rename",
           "computer name", "hostname", "specifications"),
          heading="About", description="System overview: version, hardware, current load and the state of the "
                                       "application runtimes."),
    _page("system", "install", "Install Boswas OS", "Restart into the installer of this live session",
          ("boswas-install", "system-software-install-symbolic", "drive-harddisk"), ("installer",),
          description="You are using Boswas OS as a live session from the boot medium.", live_only=True),

    # --- Bluetooth & devices ---
    _category("devices", "Bluetooth & devices",
              "Bluetooth, printers, cameras, the mouse, touchpad and keyboard, and the hardware inside.",
              ("preferences-desktop-peripherals", "preferences-system-bluetooth"),
              ("peripherals", "devices", "hardware")),
    _page("devices", "bluetooth", "Bluetooth", "Pair, connect and remove Bluetooth devices",
          ("preferences-system-bluetooth", "bluetooth-active-symbolic"),
          ("pair", "wireless", "headphones"),
          description="Pair headphones, keyboards, phones and other Bluetooth devices."),
    _page("devices", "printers", "Printers", "Printers, the default printer, a test page",
          ("printer", "document-print-symbolic"), ("printer", "print", "cups", "test page", "printing"),
          description="The printers this device knows. Printers on the network that support driverless printing "
                      "appear by themselves."),
    _page("devices", "cameras", "Cameras", "Connected cameras and the Camera application",
          ("camera-web", "camera-photo"), ("webcam", "camera", "video"),
          description="The cameras connected to this device.", programs=(CAMERA,), programs_title="Camera"),
    _page("devices", "mouse", "Mouse", "Pointer speed, primary button, scrolling",
          ("preferences-desktop-mouse", "input-mouse"), ("pointer", "cursor", "scroll", "buttons"),
          description="Pointer speed, the primary button, scrolling and the size of the pointer."),
    _page("devices", "touchpad", "Touchpad", "Taps, scrolling, speed, gestures",
          ("input-touchpad-symbolic", "preferences-desktop-peripherals"), ("trackpad", "tap", "gestures"),
          description="Taps, scrolling, speed and gestures of the touchpad."),
    _page("devices", "keyboard", "Keyboard", "Layouts, switching, repeat delay and rate",
          ("preferences-desktop-keyboard", "input-keyboard"), ("layout", "typing", "repeat", "num lock"),
          description="Keyboard layouts, the shortcut that switches them, and how keys repeat."),
    _page("devices", "usb", "USB", "The USB devices connected now",
          ("drive-removable-media-usb", "drive-removable-media"), ("usb", "plugged in", "connected devices"),
          description="The USB devices connected to this device and the driver each one uses."),
    _page("devices", "hardware", "Hardware & Drivers", "Devices inside this computer, drivers, missing firmware",
          ("hwinfo", "computer"),
          ("driver", "firmware", "pci", "usb", "device", "graphics", "wifi card", "ethernet"),
          description="The devices in this computer, the driver each one uses, and firmware that is missing."),

    # --- Network & internet ---
    _category("network", "Network & internet", "Wi-Fi, Ethernet, VPN, airplane mode and the network adapters.",
              ("preferences-system-network", "network-wired"),
              ("internet", "network", "connection", "online")),
    _page("network", "wifi", "Wi-Fi", "Networks, connect, forget, scan",
          ("network-wireless", "network-wireless-symbolic"), ("wifi", "wi-fi", "wlan", "wireless", "hotspot"),
          description="Turn Wi-Fi on or off, connect to a network and forget networks."),
    _page("network", "ethernet", "Ethernet", "Wired connection",
          ("network-wired", "network-wired-symbolic"), ("ethernet", "wired", "cable", "lan"),
          description="The wired connection of this device."),
    _page("network", "vpn", "VPN", "Add, connect and remove VPN connections",
          ("network-vpn-symbolic",), ("vpn", "wireguard", "openvpn", "virtual private network", "tunnel"),
          description="Virtual private networks of NetworkManager: WireGuard and OpenVPN connections."),
    _page("network", "airplane", "Airplane mode", "Turn off all wireless radios",
          ("airplane-mode-symbolic",), ("airplane", "flight mode", "aeroplane", "radio"),
          description="Turns off Wi-Fi, Bluetooth and every other radio at once."),
    _page("network", "proxy", "Proxy", "The proxy of this session",
          ("globe-symbolic", "network-workgroup"), ("proxy", "http_proxy", "https_proxy"),
          description="The proxy applications of this session use."),
    _page("network", "advanced-network", "Advanced network settings",
          "Network adapters, data usage, hardware and connection properties",
          ("preferences-system-network", "network-workgroup"),
          ("adapter", "network adapter", "data usage", "mac address", "ip address", "dns", "gateway",
           "disable adapter", "properties"),
          description="The network adapters of this device, the data each one moved, and its addresses."),

    # --- Personalization ---
    _category("personalization", "Personalization", "Background, colours, themes, Start, the taskbar and fonts.",
              ("preferences-desktop-theme-global", "preferences-desktop-wallpaper"),
              ("theme", "look", "appearance", "customise", "customize")),
    _page("personalization", "background", "Background", "Background picture",
          ("preferences-desktop-wallpaper", "image-x-generic-symbolic"),
          ("wallpaper", "background", "desktop picture", "photo"),
          description="The picture behind your windows."),
    _page("personalization", "colours", "Colours", "Dark and light mode, accent colour, transparency",
          ("preferences-desktop-theme-global",), ("colour", "color", "colors", "dark", "light", "mode"),
          heading="Colours", description="Dark or light, the accent colour, and whether the taskbar and flyouts "
                                         "let the background show through."),
    _page("personalization", "themes", "Themes", "Boswas presets: background, colours and look in one step",
          ("preferences-desktop-theme-global", "boswas-os"), ("preset", "theme", "look", "style"),
          description="Give your desktop a new look with a Boswas preset."),
    _page("personalization", "start", "Start", "Layout, recommendations, folders",
          ("boswas-logo", "view-app-grid-symbolic"),
          ("start menu", "pinned", "recommended", "recently added", "most used", "folders", "power button"),
          description="What Start shows: pinned applications, recommendations and folders next to the power button."),
    _page("personalization", "taskbar", "Taskbar", "Alignment, Task View, automatically hide, badges, seconds",
          ("boswas-shell", "view-grid-symbolic"),
          ("taskbar", "panel", "alignment", "align left", "auto hide", "task view", "badges", "clock seconds"),
          description="Where the taskbar's buttons sit, what it shows, and whether it hides itself."),
    _page("personalization", "fonts", "Fonts", "Install, preview and remove fonts",
          ("preferences-desktop-font", "font-x-generic"), ("font", "typeface", "ttf", "otf"),
          description="The fonts installed on this device. Fonts you add are installed for your account only."),

    # --- Apps ---
    _category("apps", "Apps", "Installed apps, the Software Center, default and start-up apps, optional features.",
              ("boswas-software-center", "view-app-grid-symbolic"), ("applications", "programs", "software")),
    _page("apps", "installed-apps", "Installed apps", "Uninstall and manage your applications",
          ("view-app-grid-symbolic", "boswas-software-center"),
          ("uninstall", "remove", "apps", "programs", "installed", "size"),
          description="Every application on this device, where it comes from, and how to remove it."),
    _page("apps", "applications", "Software Center", "Find and install Linux software, Flatpak apps and AppImages",
          ("boswas-software-center", "boswas-store"),
          ("apps", "software", "programs", "install", "remove", "deb", "debian", "flatpak", "flathub", "appimage",
           "store"),
          heading="Boswas Software Center",
          description="Your applications in one place: open them, and install Linux software, Flatpak "
                      "applications and AppImages."),
    _page("apps", "default-apps", "Default apps", "The apps that open links and files",
          ("preferences-system-windows-actions", "preferences-system"), ("default", "open with", "browser"),
          description="Choose which application opens each kind of file and link."),
    _page("apps", "startup", "Startup", "Apps that start when you sign in",
          ("system-run-symbolic",), ("startup", "start-up", "autostart", "sign in", "login"),
          description="Applications that start by themselves when you sign in."),
    _page("apps", "optional-features", "Optional features", "Extra codecs, printing, language support, tools",
          ("package-x-generic", "run-install-symbolic"),
          ("features", "codecs", "extras", "language pack", "add-ons", "7zip"),
          description="Optional parts of Boswas OS from Debian's signed repositories. Installing and removing "
                      "them needs an administrator password."),
    _page("apps", "windows", "Windows Compatibility", "Run Windows applications, each in its own sandbox",
          ("boswas-compat-manager", "wine"), ("wine", "exe", "msi", "32-bit", "64-bit"),
          description="The environment that runs Windows applications on Boswas OS, each in its own sandbox.",
          programs=(COMPAT_MANAGER,), programs_title="Manage"),
    _page("apps", "android", "Android", "Android apps through the optional Waydroid runtime",
          ("phone", "boswas-control-center"),
          ("android", "apk", "waydroid", "f-droid", "fdroid", "google play", "play store", "mobile"),
          heading="Android Applications",
          description="Run Android apps in Waydroid, an optional Android runtime with its own Android system."),

    # --- Accounts ---
    _category("accounts", "Accounts", "Your account and the other people who use this device.",
              ("system-users", "preferences-system-users"), ("account", "user", "sign-in")),
    _page("accounts", "users", "Your info & other users", "Name, picture, password, other users",
          ("preferences-system-users", "avatar-default-symbolic"),
          ("account", "password", "picture", "user", "family", "add user"),
          heading="Users", description="Your account, its password and picture, and the other people who use "
                                       "this device."),
    _page("accounts", "work", "Access work or school", "Whether your organisation manages this device",
          ("boswas-device-agent", "preferences-system"),
          ("work", "school", "organisation", "organization", "enrolment", "enrollment", "device management", "mdm"),
          description="Whether your organisation manages this device through the Boswas device agent."),

    # --- Time & language ---
    _category("time-language", "Time & language", "Date, time, time zone, language and regional formats.",
              ("preferences-system-time", "preferences-desktop-locale"), ("clock", "language", "region")),
    _page("time-language", "date-time", "Date & time", "Time zone, automatic clock",
          ("preferences-system-time", "preferences-system-time-symbolic"),
          ("date", "time", "clock", "time zone", "ntp"),
          description="The clock of this device and its time zone."),
    _page("time-language", "region", "Language & region", "Display language, regional format",
          ("preferences-desktop-locale", "globe-symbolic"), ("language", "region", "locale", "format"),
          description="The language of the system and how dates, times and numbers are written."),

    # --- Accessibility ---
    _category("accessibility", "Accessibility", "Settings that make the device easier to see and use.",
              ("preferences-desktop-accessibility", "preferences-desktop-accessibility-symbolic"),
              ("a11y", "ease of access", "vision")),
    _page("accessibility", "text-size", "Text size", "Make text bigger without changing anything else",
          ("preferences-desktop-font", "draw-text-symbolic"),
          ("text size", "font size", "bigger text", "larger text", "make text bigger"),
          description="Text in the taskbar, Start, title bars and applications can be up to 225 % of its size."),
    _page("accessibility", "visual-effects", "Visual effects", "Scroll bars, transparency, animations, notifications",
          ("view-layers-symbolic", "preferences-desktop-accessibility"),
          ("animation", "motion", "reduce motion", "effects", "scroll bars", "transparency", "notification time"),
          description="Scroll bars, transparency and animations, and how long notifications stay."),
    _page("accessibility", "mouse-pointer", "Mouse pointer", "Pointer size",
          ("input-mouse", "input-mouse-symbolic"), ("pointer size", "cursor size", "bigger pointer"),
          description="Make the pointer easier to see."),

    # --- Privacy & security ---
    _page("accessibility", "magnifier", "Magnifier", "Zoom in, zoom increment",
          ("zoom-in-symbolic",), ("magnifier", "zoom", "magnify", "enlarge screen", "lens"),
          description="Enlarge the screen around the pointer: Super+Plus starts the magnifier, Super+Esc ends it."),
    _page("accessibility", "color-filters", "Colour filters", "Greyscale, inverted, colour blindness filters",
          ("eye-open-symbolic",),
          ("colour filters", "color filters", "grayscale", "greyscale", "invert colours", "colour blindness",
           "color blindness", "deuteranopia", "protanopia", "tritanopia"),
          description="Show the whole screen through a filter that makes it easier to see."),
    _page("accessibility", "accessibility-keyboard", "Keyboard", "Sticky keys, filter keys",
          ("input-keyboard", "input-keyboard-symbolic"),
          ("sticky keys", "filter keys", "slow keys", "bounce keys", "modifier keys", "keyboard accessibility"),
          description="Press shortcuts one key at a time, and ignore brief or repeated keystrokes."),
    _category("privacy-security", "Privacy & security",
              "Protection of this device, the screen lock, what apps may use, and what this device shares.",
              ("preferences-system-privacy", "security-high-symbolic"), ("privacy", "security", "protection")),
    _page("privacy-security", "security", "Boswas Security Center", "Firewall, encryption, secure boot and more",
          ("boswas-security-center", "security-high-symbolic", "preferences-security-firewall"),
          ("firewall", "apparmor", "encryption", "secure boot", "antivirus", "protection"),
          description="How this device is protected. The checks run locally on this device."),
    _page("privacy-security", "lock-screen", "Lock screen", "When the screen locks by itself",
          ("system-lock-screen-symbolic", "boswas-lock"), ("lock", "screen lock", "idle"),
          description="When the screen locks by itself."),
    _page("privacy-security", "app-permissions", "App permissions",
          "Camera, microphone, location, screenshots and more for sandboxed apps",
          ("changes-prevent-symbolic", "security-high-symbolic"),
          ("permission", "camera", "microphone", "location", "screenshot", "background", "flatpak", "portal",
           "revoke"),
          description="What sandboxed (Flatpak) applications were allowed through the desktop portals."),
    _page("privacy-security", "recent-files", "Activity history", "The list of recently used files",
          ("document-open-recent-symbolic",), ("history", "recent", "activity"),
          description="What this device remembers about your activity, and how to clear it."),
    _page("privacy-security", "privacy", "Diagnostics & feedback", "What this device sends, and what it never does",
          ("preferences-system-privacy", "security-medium"),
          ("telemetry", "data", "inventory", "diagnostics", "feedback", "privacy statement"),
          description="What this device shares with your organisation, and what Boswas OS never collects."),

    # --- Boswas Update ---
    _category("updates", "Boswas Update", "How Boswas OS keeps this device up to date. Check for updates and "
              "install them.",
              ("boswas-update-center", "system-software-update"),
              ("upgrade", "security updates", "patch", "check for updates", "update"), heading="Boswas Update"),
    _page("updates", "update-history", "Update history", "What was installed, upgraded and removed",
          ("document-open-recent-symbolic", "view-list-details-symbolic"),
          ("history", "installed updates", "log"),
          description="What the package manager installed, upgraded and removed, newest first."),
    _page("updates", "update-options", "Advanced options", "Automatic updates and where updates come from",
          ("preferences-system-symbolic", "preferences-system"),
          ("automatic updates", "unattended upgrades", "update settings"),
          description="How and when this device updates itself."),
)
PAGE_KEYS = tuple(page.key for page in PAGES)
BY_KEY = {page.key: page for page in PAGES}
CATEGORIES = tuple(page for page in PAGES if page.is_category)
CATEGORY_KEYS = tuple(page.key for page in CATEGORIES)
PROGRAM_IDS = frozenset(p.id for page in PAGES for p in page.programs)


def children(key: str) -> tuple[PageInfo, ...]:
    """The pages of a category, in the order of their cards ("" for a page or an unknown key)."""
    return tuple(page for page in PAGES if page.parent == key)


def category_of(key: str) -> str:
    """The category a place belongs to (a category belongs to itself)."""
    info = BY_KEY[key]
    return info.parent or info.key


def path(key: str) -> tuple[str, ...]:
    """The headings above a place: ("System", "Display") for a page, ("System",) for a category."""
    info = BY_KEY[key]
    return (BY_KEY[info.parent].heading, info.heading) if info.parent else (info.heading,)


# --- deep links of earlier versions ---------------------------------------------------------------------

# Where the sections of a page of earlier versions went: `--page KEY --section NAME` looks for the section on
# these pages too (besides KEY itself and, for a category, its pages). The keys themselves all still exist.
MOVED_SECTIONS = {
    "system": ("date-time", "region", "about"),
    "personalization": ("colours", "background", "themes", "visual-effects"),
    "security": ("lock-screen",),
    "applications": ("default-apps", "startup"),
    "privacy": ("recent-files",),
    "network": ("wifi", "ethernet", "airplane", "advanced-network", "proxy", "vpn"),
    "devices": ("mouse", "touchpad", "keyboard"),
}
# Section names of earlier versions that a section of today answers to.
SECTION_ALIASES = {"other-connections": "ethernet"}


def section_homes(key: str) -> tuple[str, ...]:
    """The places where `--page KEY --section NAME` looks for the section, in order."""
    found = [key, *MOVED_SECTIONS.get(key, ()), *(page.key for page in children(key))]
    return tuple(dict.fromkeys(found))


# --- search -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Setting:
    """A single setting search finds by name, inside the section that holds it."""
    title: str
    page: str
    section: str = ""                # the section's command-line name, "" for the top of the page
    keywords: tuple[str, ...] = ()


SETTINGS = (
    Setting("Dark mode", "colours", "colour-mode", ("dark theme", "night mode", "light mode")),
    Setting("Night light", "display", "night-light", ("blue light", "warm colours")),
    Setting("Screen resolution", "display", "monitors", ("resolution", "screen size")),
    Setting("Refresh rate", "display", "monitors", ("hz", "frame rate")),
    Setting("Rename this device", "about", "device-name", ("computer name", "hostname", "device name", "pc name")),
    Setting("Device specifications", "about", "", ("processor", "ram", "system type")),
    Setting("Empty the Trash", "storage", "cleanup-recommendations", ("recycle bin", "trash", "deleted files")),
    Setting("Temporary files", "storage", "cleanup-recommendations", ("cache", "clean up", "free up space")),
    Setting("Change the time zone", "date-time", "date-time", ("time zone", "timezone")),
    Setting("Set the time automatically", "date-time", "date-time", ("ntp", "time sync")),
    Setting("Display language", "region", "region-language", ("language", "translation")),
    Setting("Change your password", "users", "your-account", ("password", "sign-in options")),
    Setting("Account picture", "users", "your-account", ("avatar", "photo")),
    Setting("Add a user", "users", "your-account", ("new account", "family", "other users")),
    Setting("Default browser", "default-apps", "default-applications", ("web browser", "browser")),
    Setting("Uninstall an app", "installed-apps", "", ("remove program", "uninstall")),
    Setting("Check for updates", "updates", "", ("update", "upgrade", "patch")),
    Setting("Pair a Bluetooth device", "bluetooth", "bluetooth", ("add device", "headphones", "pair")),
    Setting("Volume", "sound", "output", ("loudness", "speakers")),
    Setting("Microphone", "sound", "input", ("mic", "recording")),
    Setting("Battery saver", "power", "power-mode", ("power saver", "battery")),
    Setting("Screen brightness", "power", "brightness", ("brightness", "dim")),
    Setting("Animation effects", "visual-effects", "motion", ("animations", "reduce motion")),
    Setting("Accent colour", "colours", "accent-colour", ("accent color", "highlight colour")),
    Setting("Automatically hide the taskbar", "taskbar", "taskbar-behaviours", ("auto hide", "hide taskbar")),
    Setting("Taskbar alignment", "taskbar", "taskbar-behaviours", ("align left", "centre taskbar")),
    Setting("Do not disturb", "notifications", "do-not-disturb", ("quiet hours", "silence notifications")),
    Setting("Snap layouts", "multitasking", "snap-windows", ("split screen", "side by side")),
    Setting("Sticky keys", "accessibility-keyboard", "keyboard", ("modifier keys", "one key at a time")),
    Setting("Always show scroll bars", "visual-effects", "visual-effects", ("scrollbars",)),
    Setting("Pointer size", "mouse-pointer", "mouse-pointer", ("cursor size", "bigger pointer")),
    Setting("Lock automatically", "lock-screen", "lock-screen", ("screen lock", "idle")),
    Setting("Clear activity history", "recent-files", "recent-files", ("recent files", "history")),
    Setting("Firmware settings", "recovery", "advanced-start-up", ("uefi", "bios", "advanced startup")),
    Setting("Network troubleshooter", "troubleshoot", "troubleshooters", ("no internet", "internet problem")),
    Setting("Install a font", "fonts", "fonts", ("add font", "ttf", "otf")),
    Setting("Import a VPN connection", "vpn", "vpn-connections", ("wireguard", "openvpn", "add vpn")),
    Setting("Disable a network adapter", "advanced-network", "network-adapters", ("adapter", "disable")),
    Setting("Print a test page", "printers", "printers", ("test page", "printer")),
    Setting("Camera permissions", "app-permissions", "app-permissions", ("camera access", "webcam access")),
)


@dataclass(frozen=True)
class Entry:
    """One thing search can find: a place, a section of a page, or a single setting."""
    kind: str                        # "category", "page", "section" or "setting"
    title: str
    page: str                        # the place it opens
    section: str = ""                # the section it scrolls to
    keywords: tuple[str, ...] = ()


def entries(sections: list[tuple[str, str, str, tuple[str, ...]]] = ()) -> list[Entry]:
    """Everything search can find. ``sections``: (title, page, command-line name, keywords) of each section."""
    found = [Entry("category" if page.is_category else "page", page.label, page.key, "",
                   (page.heading, *page.keywords)) for page in PAGES]
    for title, page, name, words in sections:
        if page not in BY_KEY:
            continue
        if title.casefold() in (BY_KEY[page].label.casefold(), BY_KEY[page].heading.casefold()):
            # The page has the section's name: one result, the page, which also finds the section's words.
            found = [Entry(e.kind, e.title, e.page, e.section, (*e.keywords, *words)) if e.page == page
                     and e.kind == "page" else e for e in found]
            continue
        found.append(Entry("section", title, page, name, tuple(words)))
    found += [Entry("setting", s.title, s.page, s.section, s.keywords) for s in SETTINGS]
    return found


def _words(text: str) -> list[str]:
    return [word for word in re.split(r"[\s/,]+", text.casefold()) if word]


def search(query: str, candidates: list[Entry], visible=None, limit: int = 30) -> list[Entry]:
    """The entries that contain every word of ``query``, best first, one per (page, section, title).

    A title that is the query comes first, then titles that start with it,
    then titles that contain every word, then entries that match only by a
    keyword; within that, places before sections before single settings, and
    the order of the window. ``visible(page_key)`` leaves out what this
    device does not show.
    """
    words = _words(query)
    if not words:
        return []
    phrase = " ".join(words)
    order = {key: index for index, key in enumerate(PAGE_KEYS)}
    kinds = {"category": 0, "page": 1, "section": 2, "setting": 3}
    ranked, seen = [], set()
    for entry in candidates:
        if visible is not None and not visible(entry.page):
            continue
        title = entry.title.casefold()
        haystack = " ".join((title, *(word.casefold() for word in entry.keywords)))
        if not all(word in haystack for word in words):
            continue
        if title == phrase:
            score = 0
        elif title.startswith(phrase):
            score = 1
        elif all(word in title for word in words):
            score = 2
        else:
            score = 3
        identity = (entry.page, entry.section, title)
        if identity in seen:
            continue
        seen.add(identity)
        ranked.append(((score, kinds.get(entry.kind, 4), order.get(entry.page, 999), entry.title), entry))
    ranked.sort(key=lambda item: item[0])
    return [entry for _rank, entry in ranked[:limit]]
