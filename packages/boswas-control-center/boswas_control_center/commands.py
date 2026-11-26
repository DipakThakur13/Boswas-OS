"""The programs Control Center may start, and the only code that starts them.

Every process is started from a fixed argument list whose first element is
one of the absolute paths in EXECUTABLES. There is no shell, no search of
$PATH, no string that is split into words, and every call has a timeout:

  run(argv, timeout)          wait for a short command and capture its output
                              (boswas --json status, boswas-preset, apt-cache, ...)
  start(argv)                 start a desktop program (Files, the Compatibility
                              Manager, an application through `gio launch`) in its
                              own session, wait briefly to report an immediate
                              failure, then let it run on its own; Control Center
                              does not stay its parent
  stream(argv, on_line, ...)  run a long operation (the privileged helper
                              boswas-sysops through pkexec, boswas-android) and
                              hand every output line to on_line as it arrives;
                              the caller may ask the program to cancel by
                              writing "cancel" to its standard input

The builders below validate every variable argument before it reaches argv:
pkexec is only ever started as ``pkexec /usr/libexec/boswas/sysops
<operation> ...`` with an operation of SYSOPS_OPERATIONS and arguments that
pass that operation's validator; apt-get only ever simulates (``-s``);
boswas-android only with a command of ANDROID_COMMANDS; gio only as ``gio
launch <desktop entry>``; nmcli only for queries; ip only with the three
read-only JSON queries of IP_QUERIES; timedatectl and localectl only with
the subcommands and validated values of timedatectl_argv() and
localectl_argv(), hostnamectl only to set the static host name
(hostnamectl_argv()), systemctl only for the restarts and the one query of
SYSTEMCTL_FORMS (systemd's own services ask polkit for the changes) and to
give the user manager the pointer size (cursor_environment_argv(), with
dbus-update-activation-environment for D-Bus activation: XCURSOR_SIZE=N,
nothing else).

nmcli changes only what nmcli_change_argv() builds: a connection up, down,
deleted or imported from a file the user chose, an adapter connected or
disconnected, and a wired connection's addresses, DNS servers and metered
state (connection modify uuid UUID with the properties of NMCLI_PROPERTIES,
every value validated, addresses with the ipaddress module); NetworkManager
asks polkit, and a VPN's secret is asked by NetworkManager's agent, never put
on a command line. udisksctl only unmounts, locks and powers off a block
device of /dev (udisksctl_argv(): the USB page's Eject; UDisks2 asks polkit),
and boswas-files opens a folder by its absolute path (files_argv()).
fc-list and fc-cache only in their fixed forms (the font list; the cache of
the user's own font folder), lpstat only to list printers, lp only to print CUPS's own test page
(TEST_PAGE) on a validated printer, dpkg-query only to read which package
owns a file and which packages are installed, flatpak's permission store only
to list and to remove one application's permission (permission_argv()).

busctl only talks to AccountsService, with the methods and validated
arguments of accounts_argv() (user names, numbers, a full name, a file
path: never a password or a password hash), and asks logind the two
read-only questions of logind_argv(). passwd is started without
arguments (the user's own password) on a pseudo-terminal by converse(),
which types the answers of a conversation object and never keeps or logs
what was typed or printed.

The services bridge (/usr/libexec/boswas/boswas-services-bridge) is the one
program not started here: it is a long-lived child that speaks on pipes,
started without arguments by ui/bridgeclient.py.
"""

from __future__ import annotations

import ipaddress
import os
import re
import select
import subprocess
import termios
import threading
import time
from dataclasses import dataclass

from .errors import CommandFailed, CommandUnavailable

# name -> absolute path. Nothing else is ever executed.
EXECUTABLES = {
    "boswas": "/usr/bin/boswas",                                  # boswas-cli: posture checks
    "boswas-preset": "/usr/bin/boswas-preset",                    # desktop presets
    "boswas-compat-manager": "/usr/bin/boswas-compat-manager",    # Windows applications
    "boswas-android": "/usr/bin/boswas-android",                  # Android runtime manager (boswas-android)
    "boswas-files": "/usr/bin/boswas-files",                      # Files (boswas-apps): browse a folder
    "gio": "/usr/bin/gio",                                        # libglib2.0-bin: only `gio launch <entry>`
    "ip": "/usr/bin/ip",                                          # iproute2: only the JSON queries of IP_QUERIES
    "timedatectl": "/usr/bin/timedatectl",                        # systemd: time zone, NTP, clock (polkit)
    "localectl": "/usr/bin/localectl",                            # systemd: system locale (polkit)
    "locale": "/usr/bin/locale",                                  # libc-bin: only `locale -a`
    "busctl": "/usr/bin/busctl",                                  # systemd: only AccountsService calls
    "passwd": "/usr/bin/passwd",                                  # passwd: the own password, on a pseudo-terminal
    "apt-config": "/usr/bin/apt-config",                          # only `apt-config shell` (read-only)
    "apt-get": "/usr/bin/apt-get",                                # only `apt-get -s ...` (simulation, read-only)
    "apt-cache": "/usr/bin/apt-cache",                            # only search, show, policy (read-only)
    "flatpak": "/usr/bin/flatpak",                                # user installation and read-only queries
    "nmcli": "/usr/bin/nmcli",                                    # NetworkManager: queries only (-t)
    "lsblk": "/usr/bin/lsblk",                                    # disks (read-only, no serial numbers)
    "systemctl": "/usr/bin/systemctl",                            # only the forms of SYSTEMCTL_FORMS
    "pkexec": "/usr/bin/pkexec",                                  # only `pkexec SYSOPS <operation> ...`
    "hostnamectl": "/usr/bin/hostnamectl",                        # systemd: the static host name (polkit)
    "fc-list": "/usr/bin/fc-list",                                # fontconfig: the installed fonts (read-only)
    "fc-cache": "/usr/bin/fc-cache",                              # fontconfig: the cache of the user's fonts
    "lpstat": "/usr/bin/lpstat",                                  # cups-client: printers (read-only)
    "lp": "/usr/bin/lp",                                          # cups-client: only CUPS's own test page
    "dpkg-query": "/usr/bin/dpkg-query",                          # dpkg: owners of files, installed packages
    "boswas-camera": "/usr/bin/boswas-camera",                    # Camera (boswas-apps)
    "udisksctl": "/usr/bin/udisksctl",                            # udisks2: only udisksctl_argv() (Eject)
    "dbus-update-activation-environment": "/usr/bin/dbus-update-activation-environment",
    # dbus-bin: only XCURSOR_SIZE=N (cursor_environment_argv())
}
ALLOWED_PATHS = frozenset(EXECUTABLES.values())

# The privileged helper (package boswas-sysops) and the operations Control Center uses
# (docs/architecture/platform-contracts.md, section 1). Android operations go through
# boswas-android, which calls the helper itself.
SYSOPS = "/usr/libexec/boswas/sysops"
ADMIN_TOOL = "pkexec"
NO_ADMIN = ("Administrator operations are not available on this system (polkit's authorization tool, pkexec, is "
            "not installed).")
SYSOPS_OPERATIONS = frozenset({
    "apt-refresh", "apt-upgrade", "apt-install", "apt-remove", "apt-install-local", "apt-clean",
    "journal-vacuum", "firmware-report", "flatpak-remote-enable", "flatpak-remote-disable",
})
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,99}(:(amd64|i386|all))?$")
# apt-install's one option (boswas-sysops 3.0~alpha1 and later): leave out what the packages only recommend.
NO_RECOMMENDS = "--no-recommends"
# flatpak: read-only queries of both installations; changes only to the user's own installation.
FLATPAK_SUBCOMMANDS = frozenset({"remotes", "list", "search", "remote-add", "install", "uninstall", "update",
                                 "permission-list"})
FLATPAK_USER_ONLY = frozenset({"remote-add", "install", "uninstall", "update"})
REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_PACKAGES = 50

# ip (iproute2): the only argument lists used, all read-only with JSON output.
IP_QUERIES = frozenset({("-j", "addr", "show"), ("-j", "-4", "route", "show"), ("-j", "-6", "route", "show")})
# timedatectl and localectl values. A time zone is a name of the tz database; a locale is a glibc locale name.
TIMEZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]{0,31}(/[A-Za-z0-9][A-Za-z0-9_+-]{0,31}){0,2}$")
TIME_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$")
LOCALE_RE = re.compile(r"^[A-Za-z]{1,8}(_[A-Za-z0-9]{1,8})?(\.[A-Za-z0-9-]{1,16})?(@[A-Za-z0-9]{1,16})?$")
LOCALE_VARIABLES = ("LANG", "LC_NUMERIC", "LC_TIME", "LC_MONETARY", "LC_PAPER", "LC_MEASUREMENT", "LC_COLLATE",
                    "LC_NAME", "LC_ADDRESS", "LC_TELEPHONE")

# AccountsService (busctl): the service, its object paths, and what a user name looks like.
ACCOUNTS_SERVICE = "org.freedesktop.Accounts"
ACCOUNTS_PATH = "/org/freedesktop/Accounts"
ACCOUNTS_USER_RE = re.compile(r"^/org/freedesktop/Accounts/User[0-9]{1,10}$")
ACCOUNTS_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
ACCOUNTS_METHODS = frozenset({"ListCachedUsers", "CreateUser", "DeleteUser", "GetAll", "SetRealName",
                              "SetAccountType", "SetIconFile", "SetLocked"})
CONVERSE_TIMEOUT = 60.0

# logind (busctl): the two questions the Recovery page asks, both read-only.
LOGIND_SERVICE = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
LOGIND_INTERFACE = "org.freedesktop.login1.Manager"
LOGIND_QUESTIONS = frozenset({"CanRebootToFirmwareSetup", "CanRebootToBootLoaderMenu"})

# systemctl: the only argument lists used. The restarts are asked by logind, which asks polkit.
SYSTEMCTL_FORMS = frozenset({("reboot",), ("reboot", "--firmware-setup"), ("reboot", "--boot-loader-menu=0"),
                             ("is-active", "bluetooth.service")})

# hostnamectl: a static host name of 1-63 letters, digits and hyphens, not starting or ending with a hyphen.
HOSTNAME_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")

# nmcli: the words that make a change; a query (-t) never contains one of them.
NMCLI_CHANGE_WORDS = frozenset({"up", "down", "delete", "import", "connect", "disconnect", "modify", "add", "on",
                                "off", "reload", "edit", "clone", "monitor", "load", "reapply", "set", "wifi-hotspot",
                                "--show-secrets", "-s", "--ask", "-a", "rescan", "export"})
NMCLI_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
NMCLI_IMPORT_TYPES = frozenset({"openvpn", "wireguard"})
IFNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,14}$")
MAX_DNS_SERVERS = 3
# The properties `nmcli connection modify uuid UUID ...` may set (the Ethernet page), each with its check.
NMCLI_PROPERTIES = {
    "ipv4.method": lambda value: value in ("auto", "manual"),
    "ipv4.addresses": lambda value: value == "" or _ipv4_interface(value),
    "ipv4.gateway": lambda value: value == "" or _ip_address(value, 4),
    "ipv4.dns": lambda value: _address_list(value, 4),
    "ipv4.ignore-auto-dns": lambda value: value in ("yes", "no"),
    "ipv6.dns": lambda value: _address_list(value, 6),
    "ipv6.ignore-auto-dns": lambda value: value in ("yes", "no"),
    "connection.metered": lambda value: value in ("yes", "no", "unknown"),
}

# udisksctl: the steps of Eject, each on one block device of /dev (a disk, a partition or an unlocked volume).
UDISKS_ACTIONS = frozenset({"unmount", "lock", "power-off"})
BLOCK_DEVICE_RE = re.compile(r"^/dev/(sd[a-z]{1,3}[0-9]{0,3}|mmcblk[0-9]{1,3}(p[0-9]{1,3})?|dm-[0-9]{1,4}|"
                             r"nvme[0-9]{1,3}n[0-9]{1,3}(p[0-9]{1,3})?)$")
# The pointer size the user manager and D-Bus activation pass to what they start.
CURSOR_SIZES = (16, 96)

# fontconfig and CUPS.
FONT_LIST_FORMAT = "--format=%{family[0]}\\t%{style[0]}\\t%{file}\\n"
TEST_PAGE = "/usr/share/cups/data/testprint"
PRINTER_RE = re.compile(r"^[A-Za-z0-9_.@][A-Za-z0-9_.@+-]{0,126}$")
LPSTAT_FORMS = frozenset({("-e",), ("-p", "-d")})

# dpkg-query: who owns a file (-S), and the state and size of packages (-W with this format).
DPKG_STATUS_FORMAT = "--showformat=${Package}\\t${db:Status-Abbrev}\\t${Installed-Size}\\t${Version}\\n"
MAX_DPKG_ARGS = 400

# The flatpak permission store: table and object names as xdg-desktop-portal writes them.
PERMISSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
FLATPAK_APP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*){2,}$")

# boswas-android commands (contract section 2): words after `boswas-android --json`.
ANDROID_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
ANDROID_COMMANDS = {
    ("status",): 0, ("check",): 0, ("google-play",): 0, ("app", "list"): 0,
    ("session", "start"): 0, ("session", "stop"): 0, ("fdroid", "install"): 0,
    ("runtime", "install"): 0, ("runtime", "init"): 0, ("runtime", "update"): 0, ("runtime", "repair"): 0,
    ("runtime", "remove"): 0, ("service", "start"): 0, ("service", "stop"): 0, ("service", "restart"): 0,
    ("apk", "inspect"): 1, ("apk", "install"): 1, ("app", "launch"): 1, ("app", "remove"): 1,
}

DEFAULT_TIMEOUT = 30.0
START_SETTLE = 1.5          # seconds a started program may take to fail visibly
MAX_OUTPUT = 4 * 1024 * 1024
MAX_LINE = 64 * 1024        # a streamed line longer than this is cut
STREAM_TIMEOUT = 4 * 3600.0


@dataclass(frozen=True)
class Completed:
    code: int
    stdout: str
    stderr: str


def _plain(arg: object) -> bool:
    return isinstance(arg, str) and bool(arg) and "\0" not in arg and "\n" not in arg and len(arg) <= 4096


def argv(name: str, *args: str) -> list[str]:
    """[absolute path of name, *args] after checking each argument is a plain single string."""
    path = EXECUTABLES.get(name)
    if path is None:
        raise ValueError(f"{name!r} is not an allowed program")
    if name == "pkexec":
        raise ValueError("pkexec is started only through sysops_argv()")
    if name == "apt-get" and (not args or args[0] != "-s"):
        raise ValueError("apt-get is only ever run as a simulation (-s)")
    if name == "apt-cache" and (not args or args[0] not in ("search", "show", "policy")):
        raise ValueError("apt-cache is only used for search, show and policy")
    if name == "nmcli" and (not args or args[0] != "-t" or any(a in NMCLI_CHANGE_WORDS for a in args)):
        raise ValueError("nmcli is used for queries (-t) only; changes are built by nmcli_change_argv()")
    if name == "gio" and (len(args) != 2 or args[0] != "launch" or not _desktop_entry_path(args[1])):
        raise ValueError("gio is only used as `gio launch <absolute path of a desktop entry>`")
    if name == "ip" and tuple(args) not in IP_QUERIES:
        raise ValueError("ip is used for the read-only JSON queries of addresses and routes only")
    if name == "locale" and tuple(args) != ("-a",):
        raise ValueError("locale is only used as `locale -a`")
    if name == "systemctl" and tuple(args) not in SYSTEMCTL_FORMS:
        raise ValueError("systemctl is used for the restarts and the query of SYSTEMCTL_FORMS only")
    if name == "fc-list" and tuple(args) != (FONT_LIST_FORMAT,):
        raise ValueError("fc-list is only used with its fixed format")
    if name == "lpstat" and tuple(args) not in LPSTAT_FORMS:
        raise ValueError("lpstat is only used to list printers")
    if name in ("hostnamectl", "fc-cache", "lp", "dpkg-query", "udisksctl", "dbus-update-activation-environment"):
        raise ValueError(f"{name} is started only through its builder")
    if name in ("timedatectl", "localectl"):
        raise ValueError(f"{name} is started only through {name}_argv()")
    if name == "busctl":
        raise ValueError("busctl is started only through accounts_argv()")
    if name == "passwd":
        raise ValueError("passwd is started only through passwd_argv(), on a pseudo-terminal")
    if name == "flatpak":
        if not args or args[0] not in FLATPAK_SUBCOMMANDS:
            raise ValueError(f"flatpak {args[:1]} is not allowed")
        if args[0] in FLATPAK_USER_ONLY and "--user" not in args:
            raise ValueError(f"flatpak {args[0]} is only used for the user's own installation (--user)")
    if name == "boswas-android":
        raise ValueError("boswas-android is started only through android_argv()")
    for arg in args:
        if not _plain(arg):
            raise ValueError(f"invalid argument for {name}: {arg!r}")
    return [path, *args]


def _desktop_entry_path(path: object) -> bool:
    return (_plain(path) and os.path.isabs(path) and path.endswith(".desktop") and ".." not in path.split("/")
            and not os.path.basename(path).startswith("-"))


def timedatectl_argv(*words: str) -> list[str]:
    """timedatectl show | set-ntp true|false | set-timezone <Area/City> | set-time "YYYY-MM-DD HH:MM:SS"."""
    words = tuple(words)
    ok = (words == ("show",)
          or (len(words) == 2 and words[0] == "set-ntp" and words[1] in ("true", "false"))
          or (len(words) == 2 and words[0] == "set-timezone" and isinstance(words[1], str)
              and bool(TIMEZONE_RE.fullmatch(words[1])) and ".." not in words[1])
          or (len(words) == 2 and words[0] == "set-time" and isinstance(words[1], str)
              and bool(TIME_RE.fullmatch(words[1]))))
    if not ok:
        raise ValueError(f"timedatectl {' '.join(map(str, words))!r} is not allowed")
    return [EXECUTABLES["timedatectl"], *words]


def localectl_argv(*words: str) -> list[str]:
    """localectl status | set-locale VAR=locale ... (variables of LOCALE_VARIABLES, locale names of LOCALE_RE)."""
    words = tuple(words)
    ok = words == ("status",)
    if not ok and len(words) >= 2 and words[0] == "set-locale" and len(words) <= 1 + len(LOCALE_VARIABLES):
        names = [w.partition("=")[0] if isinstance(w, str) else "" for w in words[1:]]
        ok = (len(set(names)) == len(names)
              and all(isinstance(w, str) and "=" in w and w.partition("=")[0] in LOCALE_VARIABLES
                      and bool(LOCALE_RE.fullmatch(w.partition("=")[2])) for w in words[1:]))
    if not ok:
        raise ValueError(f"localectl {' '.join(map(str, words))!r} is not allowed")
    return [EXECUTABLES["localectl"], *words]


def accounts_argv(method: str, *args, path: str | None = None) -> list[str]:
    """busctl --system --json=short --timeout=N -- call org.freedesktop.Accounts ...: one AccountsService call.

    Without ``path`` a method of the service itself: ListCachedUsers,
    CreateUser(name, full name, account type 0|1), DeleteUser(uid, remove
    files). With the object path of a user: GetAll (its properties),
    SetRealName(text), SetAccountType(0|1), SetIconFile(absolute path),
    SetLocked(bool). Every argument is validated; none of them is a secret.
    """
    def name_ok(value: object) -> bool:
        return isinstance(value, str) and bool(ACCOUNTS_NAME_RE.fullmatch(value))

    def text_ok(value: object) -> bool:
        return (_plain(value) and len(value) <= 256 and not value.startswith("-") and ":" not in value
                and "," not in value and all(ch.isprintable() for ch in value))

    def type_ok(value: object) -> bool:
        return value in (0, 1) and not isinstance(value, bool)

    timeout = "25"
    if path is None:
        target = [ACCOUNTS_PATH, ACCOUNTS_SERVICE]
        if method == "ListCachedUsers" and not args:
            words = [method]
        elif method == "CreateUser" and len(args) == 3 and name_ok(args[0]) and text_ok(args[1]) and type_ok(args[2]):
            words, timeout = [method, "ssi", args[0], args[1], str(args[2])], "180"
        elif method == "DeleteUser" and len(args) == 2 and isinstance(args[0], int) and not isinstance(args[0], bool) \
                and 1000 <= args[0] <= 0xFFFFFFFE and isinstance(args[1], bool):
            words, timeout = [method, "xb", str(args[0]), "true" if args[1] else "false"], "180"
        else:
            raise ValueError(f"AccountsService {method}{args!r} is not allowed")
    else:
        if not isinstance(path, str) or not ACCOUNTS_USER_RE.fullmatch(path):
            raise ValueError("not the object path of a user")
        target = [path, ACCOUNTS_SERVICE + ".User"]
        if method == "GetAll" and not args:
            target, words = [path, "org.freedesktop.DBus.Properties"], [method, "s", ACCOUNTS_SERVICE + ".User"]
        elif method == "SetRealName" and len(args) == 1 and text_ok(args[0]):
            words, timeout = [method, "s", args[0]], "180"
        elif method == "SetAccountType" and len(args) == 1 and type_ok(args[0]):
            words, timeout = [method, "i", str(args[0])], "180"
        elif method == "SetIconFile" and len(args) == 1 and _plain(args[0]) and os.path.isabs(args[0]) \
                and ".." not in args[0].split("/"):
            words, timeout = [method, "s", args[0]], "180"
        elif method == "SetLocked" and len(args) == 1 and isinstance(args[0], bool):
            words, timeout = [method, "b", "true" if args[0] else "false"], "180"
        else:
            raise ValueError(f"AccountsService user method {method}{args!r} is not allowed")
    return [EXECUTABLES["busctl"], "--system", "--json=short", f"--timeout={timeout}", "--", "call",
            ACCOUNTS_SERVICE, *target, *words]


def logind_argv(question: str) -> list[str]:
    """busctl ... call org.freedesktop.login1 ... CanRebootToFirmwareSetup | CanRebootToBootLoaderMenu (read-only)."""
    if question not in LOGIND_QUESTIONS:
        raise ValueError(f"logind {question!r} is not an allowed question")
    return [EXECUTABLES["busctl"], "--system", "--json=short", "--timeout=10", "--", "call", LOGIND_SERVICE,
            LOGIND_PATH, LOGIND_INTERFACE, question]


def valid_hostname(name: object) -> bool:
    """1-63 characters of A-Z, a-z, 0-9 and "-", not starting or ending with "-" (a static host name)."""
    return isinstance(name, str) and bool(HOSTNAME_RE.fullmatch(name))


def hostnamectl_argv(name: str) -> list[str]:
    """hostnamectl --static set-hostname NAME: the static host name only (systemd-hostnamed asks polkit)."""
    if not valid_hostname(name):
        raise ValueError("a device name has 1-63 letters, digits and hyphens and does not start or end with one")
    return [EXECUTABLES["hostnamectl"], "--static", "set-hostname", name]


def _ip_address(value: object, version: int) -> bool:
    """An IPv4 or IPv6 address as a host has it: no network, multicast, loopback or unspecified address."""
    if not isinstance(value, str) or not value or len(value) > 64 or value != value.strip():
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (address.version == version and not address.is_multicast and not address.is_unspecified
            and not address.is_loopback and str(address) == value.lower())


def _ipv4_interface(value: object) -> bool:
    """ADDRESS/PREFIX of a host in an IPv4 network: prefix 1 to 32, not the network or broadcast address."""
    if not isinstance(value, str) or value.count("/") != 1:
        return False
    address, prefix = value.split("/")
    if not _ip_address(address, 4) or not prefix.isdigit() or not 1 <= int(prefix) <= 32 or prefix != str(int(prefix)):
        return False
    network = ipaddress.ip_interface(value).network
    return int(prefix) >= 31 or ipaddress.ip_address(address) not in (network.network_address,
                                                                        network.broadcast_address)


def _address_list(value: object, version: int) -> bool:
    """"" or 1 to MAX_DNS_SERVERS addresses of one IP version, separated by commas, each once."""
    if value == "":
        return True
    if not isinstance(value, str):
        return False
    parts = value.split(",")
    return 1 <= len(parts) <= MAX_DNS_SERVERS and len(set(parts)) == len(parts) and \
        all(_ip_address(part, version) for part in parts)


def nmcli_change_argv(*words: str) -> list[str]:
    """nmcli connection up|down|delete uuid UUID, connection import type openvpn|wireguard file PATH,
    device connect|disconnect IFNAME, or connection modify uuid UUID PROPERTY VALUE... (NMCLI_PROPERTIES,
    each property once, every value checked). NetworkManager asks polkit; no secret is ever an argument."""
    words = tuple(words)
    ok = False
    if len(words) == 4 and words[:2] in (("connection", "up"), ("connection", "down"), ("connection", "delete")):
        ok = words[2] == "uuid" and isinstance(words[3], str) and bool(NMCLI_UUID_RE.fullmatch(words[3]))
    elif len(words) >= 6 and len(words) % 2 == 0 and words[:3] == ("connection", "modify", "uuid"):
        properties = words[4::2]
        ok = (isinstance(words[3], str) and bool(NMCLI_UUID_RE.fullmatch(words[3]))
              and len(set(properties)) == len(properties)
              and all(isinstance(name, str) and name in NMCLI_PROPERTIES and isinstance(value, str)
                      and NMCLI_PROPERTIES[name](value) for name, value in zip(properties, words[5::2])))
    elif len(words) == 6 and words[:3] == ("connection", "import", "type") and words[4] == "file":
        path = words[5]
        ok = (words[3] in NMCLI_IMPORT_TYPES and _plain(path) and os.path.isabs(path)
              and ".." not in path.split("/") and not os.path.basename(path).startswith("-"))
    elif len(words) == 3 and words[:2] in (("device", "connect"), ("device", "disconnect")):
        ok = isinstance(words[2], str) and bool(IFNAME_RE.fullmatch(words[2]))
    if not ok:
        raise ValueError(f"nmcli {' '.join(map(str, words))!r} is not an allowed change")
    return [EXECUTABLES["nmcli"], "-t", *words]


def udisksctl_argv(action: str, device: str) -> list[str]:
    """udisksctl unmount|lock|power-off -b DEVICE: one step of ejecting a drive (UDisks2 asks polkit)."""
    if action not in UDISKS_ACTIONS or not isinstance(device, str) or not BLOCK_DEVICE_RE.fullmatch(device):
        raise ValueError(f"udisksctl {action!r} {device!r} is not allowed")
    return [EXECUTABLES["udisksctl"], action, "-b", device]


def files_argv(folder: str) -> list[str]:
    """boswas-files FOLDER: Files shows one folder, named by its absolute path (or computer:/// for This PC)."""
    if folder != "computer:///" and (not _plain(folder) or not os.path.isabs(folder) or ".." in folder.split("/")):
        raise ValueError("Files is opened at an absolute folder")
    return [EXECUTABLES["boswas-files"], folder]


def cursor_environment_argv(program: str, size: int) -> list[str]:
    """systemctl --user set-environment XCURSOR_SIZE=N, or dbus-update-activation-environment XCURSOR_SIZE=N:
    the pointer size (16 to 96) for the programs the user manager and D-Bus activation start from now on."""
    if isinstance(size, bool) or not isinstance(size, int) or not CURSOR_SIZES[0] <= size <= CURSOR_SIZES[1]:
        raise ValueError("a pointer size is a whole number from 16 to 96")
    if program == "systemctl":
        return [EXECUTABLES["systemctl"], "--user", "set-environment", f"XCURSOR_SIZE={size}"]
    if program == "dbus-update-activation-environment":
        return [EXECUTABLES[program], f"XCURSOR_SIZE={size}"]
    raise ValueError(f"{program!r} does not set the pointer size")


def _cursor_size_of(command: list[str]) -> int:
    """The size of a command of cursor_environment_argv() (0 when it is not one)."""
    word = command[-1] if command else ""
    if not word.startswith("XCURSOR_SIZE=") or not word[13:].isdigit() or len(word) > 16:
        return 0
    return int(word[13:])


def fc_cache_argv(directory: str) -> list[str]:
    """fc-cache -f DIR, for the user's own font folder (an absolute path ending in /fonts)."""
    if (not _plain(directory) or not os.path.isabs(directory) or ".." in directory.split("/")
            or os.path.basename(directory.rstrip("/")) != "fonts"):
        raise ValueError("fc-cache only refreshes the cache of the user's font folder")
    return [EXECUTABLES["fc-cache"], "-f", directory]


def valid_printer(name: object) -> bool:
    return isinstance(name, str) and bool(PRINTER_RE.fullmatch(name))


def lp_test_page_argv(printer: str) -> list[str]:
    """lp -d PRINTER /usr/share/cups/data/testprint: CUPS's own test page, nothing else is ever printed."""
    if not valid_printer(printer):
        raise ValueError("not a printer name")
    return [EXECUTABLES["lp"], "-d", printer, TEST_PAGE]


def dpkg_query_argv(mode: str, *names: str) -> list[str]:
    """dpkg-query -S PATH... (the packages that own these files) or dpkg-query -W -f=FORMAT PACKAGE... (read-only)."""
    if not 1 <= len(names) <= MAX_DPKG_ARGS:
        raise ValueError("dpkg-query needs 1 to 400 names")
    if mode == "owners":
        if not all(_plain(n) and os.path.isabs(n) and ".." not in n.split("/") for n in names):
            raise ValueError("dpkg-query -S takes absolute paths")
        return [EXECUTABLES["dpkg-query"], "-S", *names]
    if mode == "status":
        if not all(valid_package(n) for n in names):
            raise ValueError("dpkg-query -W takes package names")
        return [EXECUTABLES["dpkg-query"], "-W", DPKG_STATUS_FORMAT, *names]
    raise ValueError(f"dpkg-query {mode!r} is not allowed")


def valid_permission_name(name: object) -> bool:
    return isinstance(name, str) and bool(PERMISSION_NAME_RE.fullmatch(name))


def permission_remove_argv(table: str, ident: str, app_id: str) -> list[str]:
    """flatpak permission-remove TABLE ID APP: take back what one application was allowed (the user's store)."""
    if not (valid_permission_name(table) and valid_permission_name(ident) and isinstance(app_id, str)
            and FLATPAK_APP_RE.fullmatch(app_id) and len(app_id) <= 255):
        raise ValueError("not a permission of an application")
    return [EXECUTABLES["flatpak"], "permission-remove", table, ident, app_id]


def passwd_argv() -> list[str]:
    """passwd without arguments: it changes the password of the user who runs it, and asks on its terminal."""
    return [EXECUTABLES["passwd"]]


def valid_package(name: object) -> bool:
    return isinstance(name, str) and bool(PACKAGE_RE.fullmatch(name))


def _validate_sysops(operation: str, args: tuple[str, ...]) -> None:
    if operation not in SYSOPS_OPERATIONS:
        raise ValueError(f"{operation!r} is not an allowed boswas-sysops operation")
    if operation == "apt-install" and args[:1] == (NO_RECOMMENDS,):
        args = args[1:]                         # the one option, first and once
    if operation in ("apt-install", "apt-remove"):
        if not 1 <= len(args) <= MAX_PACKAGES or not all(valid_package(a) for a in args):
            raise ValueError(f"{operation} needs 1-{MAX_PACKAGES} valid package names")
        if len(set(args)) != len(args):
            raise ValueError(f"{operation}: a package is named twice")
    elif operation == "apt-install-local":
        if len(args) != 1 or not _plain(args[0]) or not os.path.isabs(args[0]) or ".." in args[0].split("/"):
            raise ValueError("apt-install-local needs one absolute file path")
    elif operation in ("flatpak-remote-enable", "flatpak-remote-disable"):
        if len(args) != 1 or not REMOTE_RE.fullmatch(args[0]):
            raise ValueError(f"{operation} needs one remote name")
    elif args:
        raise ValueError(f"{operation} takes no arguments")


def sysops_argv(operation: str, *args: str) -> list[str]:
    """pkexec /usr/libexec/boswas/sysops <operation> [args]: the only way pkexec is ever started."""
    _validate_sysops(operation, tuple(args))
    return [EXECUTABLES["pkexec"], SYSOPS, operation, *args]


def android_argv(*words: str) -> list[str]:
    """boswas-android --json <command> [argument]: commands of ANDROID_COMMANDS only.

    ``runtime remove`` may carry ``--purge-data``; apk commands take one absolute file
    path, app launch/remove one Android package name.
    """
    words = tuple(words)
    extra: tuple[str, ...] = ()
    if words[:2] == ("runtime", "remove") and words[2:] == ("--purge-data",):
        words, extra = words[:2], ("--purge-data",)
    command = words[:2] if words[:2] in ANDROID_COMMANDS else words[:1]
    if command not in ANDROID_COMMANDS:
        raise ValueError(f"{' '.join(words)!r} is not an allowed boswas-android command")
    rest = words[len(command):]
    if len(rest) != ANDROID_COMMANDS[command]:
        raise ValueError(f"boswas-android {' '.join(command)} takes {ANDROID_COMMANDS[command]} argument(s)")
    if command[0] == "apk":
        path = rest[0]
        if not _plain(path) or not os.path.isabs(path) or ".." in path.split("/"):
            raise ValueError("an APK is named by its absolute path")
    elif command[0] == "app" and rest:
        if not ANDROID_PACKAGE_RE.fullmatch(rest[0]) or len(rest[0]) > 255:
            raise ValueError(f"invalid Android package name {rest[0]!r}")
    return [EXECUTABLES["boswas-android"], "--json", *command, *rest, *extra]


def program_name(command: list[str]) -> str:
    return os.path.basename(command[0]) if command else "?"


def _check(command: list[str]) -> None:
    if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
        raise ValueError("a command is a non-empty list of strings")
    if command[0] not in ALLOWED_PATHS:
        raise ValueError(f"{command[0]!r} is not an allowed program")
    if command[0] == EXECUTABLES["pkexec"]:
        if len(command) < 3 or command[1] != SYSOPS:
            raise ValueError("pkexec only starts the Boswas system helper")
        _validate_sysops(command[2], tuple(command[3:]))
    if command[0] == EXECUTABLES["apt-get"] and (len(command) < 2 or command[1] != "-s"):
        raise ValueError("apt-get is only ever run as a simulation (-s)")
    if command[0] == EXECUTABLES["busctl"] and len(command) == 10 and command[6] == LOGIND_SERVICE:
        if command != logind_argv(command[9]):
            raise ValueError("busctl only asks logind the questions of logind_argv()")
    elif command[0] == EXECUTABLES["busctl"]:
        fixed = [command[0], "--system", "--json=short"]
        if (len(command) < 10 or command[:3] != fixed or not re.fullmatch(r"--timeout=[0-9]{1,3}", command[3])
                or command[4:7] != ["--", "call", ACCOUNTS_SERVICE] or command[9] not in ACCOUNTS_METHODS
                or not (command[7] == ACCOUNTS_PATH or ACCOUNTS_USER_RE.fullmatch(command[7]))
                or command[8] not in (ACCOUNTS_SERVICE, ACCOUNTS_SERVICE + ".User", "org.freedesktop.DBus.Properties")
                or any(word in ("SetPassword", "SetPasswordHint", "SetPasswordMode") for word in command)):
            raise ValueError("busctl only calls the account methods of accounts_argv()")
    if command[0] == EXECUTABLES["passwd"] and len(command) != 1:
        raise ValueError("passwd is only started without arguments (the own password)")
    name, args = program_name(command), command[1:]
    if command[0] == EXECUTABLES["nmcli"]:
        try:
            change = args[:1] == ["-t"] and any(a in NMCLI_CHANGE_WORDS for a in args)
            expected = nmcli_change_argv(*args[1:]) if change else argv("nmcli", *args)
        except ValueError:
            expected = None
        if command != expected:
            raise ValueError("nmcli runs only the queries and the changes of nmcli_change_argv()")
    elif command[0] == EXECUTABLES["hostnamectl"] and command != hostnamectl_argv(args[-1] if args else ""):
        raise ValueError("hostnamectl only sets the static host name")
    elif command[0] == EXECUTABLES["fc-cache"] and command != fc_cache_argv(args[-1] if len(args) == 2 else ""):
        raise ValueError("fc-cache only refreshes the user's font folder")
    elif command[0] == EXECUTABLES["lp"] and command != lp_test_page_argv(args[1] if len(args) == 3 else ""):
        raise ValueError("lp only prints CUPS's test page")
    elif command[0] == EXECUTABLES["dpkg-query"]:
        mode = {"-S": "owners", "-W": "status"}.get(args[0] if args else "", "")
        names = args[1:] if mode == "owners" else args[2:]
        if not mode or command != dpkg_query_argv(mode, *names):
            raise ValueError("dpkg-query only reads owners and package states")
    elif command[0] == EXECUTABLES["udisksctl"]:
        if len(args) != 3 or args[1] != "-b" or command != udisksctl_argv(args[0], args[2]):
            raise ValueError("udisksctl only unmounts, locks and powers off a block device")
    elif command[0] == EXECUTABLES["boswas-files"]:
        if len(args) != 1 or command != files_argv(args[0]):
            raise ValueError("Files is opened at one absolute folder")
    elif command[0] == EXECUTABLES["dbus-update-activation-environment"] or (
            command[0] == EXECUTABLES["systemctl"] and args[:2] == ["--user", "set-environment"]):
        size = _cursor_size_of(command)
        program = "systemctl" if command[0] == EXECUTABLES["systemctl"] else "dbus-update-activation-environment"
        try:
            expected = cursor_environment_argv(program, size)
        except ValueError:
            expected = None
        if command != expected:
            raise ValueError("only the pointer size is passed to the user manager and D-Bus activation")
    elif command[0] == EXECUTABLES["flatpak"] and args[:1] == ["permission-remove"]:
        if len(args) != 4 or command != permission_remove_argv(*args[1:]):
            raise ValueError("flatpak permission-remove takes a table, an object and an application")
    elif name in ("systemctl", "fc-list", "lpstat", "locale", "ip", "gio") and command != argv(name, *args):
        raise ValueError(f"{name} is used in its fixed forms only")


def _tail(text: str, limit: int = 400) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    tail = " ".join(lines[-3:])
    return tail if len(tail) <= limit else "…" + tail[-limit:]


class StreamHandle:
    """Control of one streamed operation, shared between the worker thread and the window.

    cancel() asks the program to stop by writing "cancel" to its standard input
    (the program decides whether it can stop safely); kill() ends it at once and is
    used only while the program says it can be cancelled.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stdin = None
        self._proc = None
        self.cancel_requested = False
        self.killed = False
        self.finished = False

    def attach(self, proc) -> None:
        with self._lock:
            self._proc, self._stdin = proc, proc.stdin
            if self.killed:
                proc.kill()

    def cancel(self) -> bool:
        with self._lock:
            if self.finished:
                return False
            self.cancel_requested = True
            if self._stdin is None:
                return True
            try:
                self._stdin.write(b"cancel\n")
                self._stdin.flush()
            except (OSError, ValueError):
                return False
            return True

    def kill(self) -> None:
        with self._lock:
            self.killed = True
            if self._proc is not None and not self.finished:
                try:
                    self._proc.kill()
                except OSError:
                    pass

    def done(self) -> None:
        with self._lock:
            self.finished = True
            if self._stdin is not None:
                try:
                    self._stdin.close()
                except OSError:
                    pass
            self._stdin = None


class CommandRunner:
    """Runs allowlisted commands (replaced by a recording fake in the tests)."""

    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ

    def available(self, path: str) -> bool:
        return path in ALLOWED_PATHS and os.access(path, os.X_OK)

    def _env(self) -> dict:
        env = dict(self.environ)
        env["LC_ALL"] = "C.UTF-8"
        return env

    def run(self, command: list[str], timeout: float = DEFAULT_TIMEOUT) -> Completed:
        """Run a short command and capture its output (C.UTF-8 locale, no stdin)."""
        _check(command)
        if command[0] == EXECUTABLES["pkexec"]:
            raise ValueError("privileged operations are streamed (stream()), never run()")
        if not timeout or timeout <= 0:
            raise ValueError("every command needs a timeout")
        name = program_name(command)
        if not self.available(command[0]):
            raise CommandUnavailable(name)
        try:
            proc = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                  env=self._env(), check=False, close_fds=True)
        except subprocess.TimeoutExpired:
            raise CommandFailed(name, f"{name} did not finish within {int(timeout)} seconds.") from None
        except OSError as exc:
            raise CommandFailed(name, f"{name} could not be started ({exc.strerror or exc}).") from None
        stdout = proc.stdout[:MAX_OUTPUT].decode("utf-8", errors="replace")
        stderr = proc.stderr[:MAX_OUTPUT].decode("utf-8", errors="replace")
        return Completed(proc.returncode, stdout, stderr)

    def converse(self, command: list[str], dialogue, timeout: float = CONVERSE_TIMEOUT) -> int:
        """Run passwd on a pseudo-terminal and let ``dialogue`` answer it; returns its exit code.

        ``dialogue.feed(text)`` gets what the program printed and returns the
        lines to type; when ``dialogue.stop`` becomes true the program is
        ended. The terminal does not echo, so nothing typed comes back as
        output; nothing typed or printed is kept or logged here. After
        `timeout` seconds the program is killed and CommandFailed is raised.
        """
        _check(command)
        if command != passwd_argv():
            raise ValueError("only passwd is run on a pseudo-terminal")
        if not timeout or timeout <= 0:
            raise ValueError("every command needs a timeout")
        name = program_name(command)
        if not self.available(command[0]):
            raise CommandUnavailable(name)
        master, slave = os.openpty()
        try:
            attributes = termios.tcgetattr(slave)
            attributes[3] &= ~(termios.ECHO | termios.ECHONL)      # what is typed is never printed back
            termios.tcsetattr(slave, termios.TCSANOW, attributes)
            env = dict(self.environ)
            env["LC_ALL"] = "C"                                      # the prompts and messages the dialogue knows
            env["LANGUAGE"] = "C"
            try:
                proc = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
                                        start_new_session=True)
            except OSError as exc:
                raise CommandFailed(name, f"{name} could not be started ({exc.strerror or exc}).") from None
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        deadline = time.monotonic() + timeout
        expired = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    expired = True
                    break
                readable, _w, _x = select.select([master], [], [], min(remaining, 0.2))
                if not readable:
                    if proc.poll() is not None:
                        break
                    continue
                try:
                    data = os.read(master, 4096)
                except OSError:                                      # the program closed its terminal: it ended
                    break
                if not data:
                    break
                for answer in dialogue.feed(data.decode("utf-8", errors="replace")):
                    os.write(master, answer.encode("utf-8") + b"\n")
                if dialogue.stop:
                    break
        finally:
            if proc.poll() is None and (expired or dialogue.stop):
                proc.kill()
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
            os.close(master)
        if expired:
            raise CommandFailed(name, f"{name} did not finish within {int(timeout)} seconds and was stopped.")
        return code

    def start(self, command: list[str], settle: float = START_SETTLE) -> None:
        """Start a desktop program detached from Control Center; raise if it fails at once."""
        _check(command)
        if command[0] == EXECUTABLES["pkexec"]:
            raise ValueError("privileged operations are streamed (stream()), never started detached")
        name = program_name(command)
        if not self.available(command[0]):
            raise CommandUnavailable(name)
        try:
            # Own session: closing Control Center does not close the program.
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, env=dict(self.environ), close_fds=True,
                                    start_new_session=True)
        except OSError as exc:
            raise CommandFailed(name, f"{name} could not be started ({exc.strerror or exc}).") from None
        try:
            code = proc.wait(timeout=settle)
        except subprocess.TimeoutExpired:
            # Still running: collect its exit status in the background so it never lingers as a zombie.
            threading.Thread(target=proc.wait, name=f"reap-{name}", daemon=True).start()
            return
        if code != 0:
            raise CommandFailed(name, f"{name} stopped right after it was started (exit code {code}).", code)

    def stream(self, command: list[str], on_line, handle: StreamHandle, timeout: float = STREAM_TIMEOUT) -> int:
        """Run a long operation, calling on_line(text) for every output line; return its exit code.

        Standard output and standard error are read together, line by line
        (lines are cut at MAX_LINE characters). The program's standard input stays
        open so handle.cancel() can ask it to stop. After `timeout` seconds the
        program is killed and CommandFailed is raised.
        """
        _check(command)
        if not timeout or timeout <= 0:
            raise ValueError("every command needs a timeout")
        name = program_name(command)
        if not self.available(command[0]):
            raise CommandUnavailable(name)
        try:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=self._env(), close_fds=True)
        except OSError as exc:
            raise CommandFailed(name, f"{name} could not be started ({exc.strerror or exc}).") from None
        handle.attach(proc)
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            proc.kill()

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            while True:
                raw = proc.stdout.readline(MAX_LINE)
                if not raw:
                    break
                on_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            code = proc.wait()
        finally:
            timer.cancel()
            handle.done()
            proc.stdout.close()
        if expired.is_set():
            raise CommandFailed(name, f"{name} did not finish within {int(timeout)} seconds and was stopped.")
        return code


def failure(command: list[str], result: Completed, what: str) -> CommandFailed:
    """A CommandFailed for a command that ran but returned an unexpected exit code."""
    name = program_name(command)
    detail = _tail(result.stderr) or _tail(result.stdout)
    message = f"{what} failed ({name} exit code {result.code})" + (f": {detail}" if detail else ".")
    return CommandFailed(name, message, result.code)
