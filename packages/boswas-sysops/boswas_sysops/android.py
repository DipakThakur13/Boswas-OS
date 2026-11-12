"""Android runtime (Waydroid): install, remove, initialise, update, repair, start/stop.

Waydroid is not part of Debian 13. Its packages come from the Waydroid
project's APT repository, described by boswas-android in
/usr/share/boswas/android/runtime.conf together with the repository's
signing key (/usr/share/boswas/keyrings/waydroid.gpg). Before APT is told to
trust that key, the keyring must contain exactly one primary key and its
fingerprint must equal KEY_FINGERPRINT; otherwise UNTRUSTED.

android-runtime-install writes (contract, section 2):
  /etc/apt/sources.list.d/boswas-waydroid.sources   deb822, Signed-By the shipped keyring
  /etc/apt/preferences.d/boswas-waydroid.pref       only ALLOWED_PACKAGES from that origin
  /etc/modprobe.d/boswas-android.conf                legacy binder devices
  /etc/modules-load.d/boswas-android.conf            load binder_linux at boot
and removes what it created if the installation fails, so a failed attempt
leaves no repository behind. The container service is not started at boot
(Android starts only when the user starts it). Images are VANILLA only:
Google components are never requested.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass

from . import apt, conffile, locks, openpgp, policy, validate
from .context import Context, Reporter
from .protocol import SysopsError
from .runner import EXECUTABLES, argv

CONF = "/usr/share/boswas/android/runtime.conf"
KEYRING = "/usr/share/boswas/keyrings/waydroid.gpg"
SOURCES = "/etc/apt/sources.list.d/boswas-waydroid.sources"
PREF = "/etc/apt/preferences.d/boswas-waydroid.pref"
MODPROBE = "/etc/modprobe.d/boswas-android.conf"
MODULES_LOAD = "/etc/modules-load.d/boswas-android.conf"
DATA_DIR = "/var/lib/waydroid"
UNIT = "waydroid-container.service"
MODULE = "binder_linux"
BINDER_DEVICES = "binder,hwbinder,vndbinder"
IMAGE_TYPE = "VANILLA"
DEFAULT_PACKAGES = ("waydroid", "python3-gbinder", "libgbinder", "libglibutil")
MIN_FREE_FOR_IMAGES = 3 * 1024 ** 3
TIMEOUT_SERVICE = 180.0
TIMEOUT_IMAGES = 6 * 3600.0
TIMEOUT_QUERY = 60.0

URL = re.compile(r"^https://([a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+)(/[A-Za-z0-9._~/-]*)?$")
TOKEN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
FINGERPRINT = re.compile(r"^([0-9A-F]{40}|[0-9A-F]{64})$")
DOWNLOAD = re.compile(r"\[Downloading\]\s*([\d.]+)\s*MB\s*/\s*([\d.]+)\s*MB")
LOG_STAMP = re.compile(r"^\[\d\d:\d\d:\d\d\]\s*")
NETWORK_TEXT = ("urlopen error", "Temporary failure in name resolution", "Network is unreachable",
                "Connection timed out", "timed out", "Name or service not known", "Connection refused",
                "Failed to download")
# Waydroid checks every downloaded image against the checksum its OTA channel
# publishes; an interrupted download (seen with SourceForge mirrors) fails it.
CHECKSUM_TEXT = ("hash doesn't match", "hash does not match")


@dataclass(frozen=True)
class RuntimeConf:
    repo_url: str
    host: str
    suite: str
    components: tuple[str, ...]
    fingerprint: str
    allowed: tuple[str, ...]
    image_type: str


def load_conf(ctx: Context) -> RuntimeConf:
    text = ctx.root.read(CONF, 65536)
    if text is None:
        raise SysopsError("UNSUPPORTED", "The Android runtime manager (boswas-android) is not installed.")
    values = conffile.parse(text)
    url = values.get("REPO_URL", "").rstrip("/")
    match = URL.fullmatch(url)
    suite = values.get("REPO_SUITE", "")
    components = tuple(values.get("REPO_COMPONENTS", "").split())
    fingerprint = openpgp.normalise(values.get("KEY_FINGERPRINT", ""))
    allowed = tuple(values.get("ALLOWED_PACKAGES", "").split())
    problems = []
    if not match:
        problems.append("REPO_URL must be an https:// URL without credentials or query")
    if not TOKEN.fullmatch(suite):
        problems.append("REPO_SUITE")
    if not components or not all(TOKEN.fullmatch(c) for c in components):
        problems.append("REPO_COMPONENTS")
    if not FINGERPRINT.fullmatch(fingerprint):
        problems.append("KEY_FINGERPRINT")
    if not allowed or not all(validate.PACKAGE.fullmatch(p) and ":" not in p for p in allowed):
        problems.append("ALLOWED_PACKAGES")
    if problems:
        raise SysopsError("FAILED", f"{CONF} is invalid ({', '.join(problems)}).")
    return RuntimeConf(url, match.group(1), suite, components, fingerprint, allowed,
                       values.get("IMAGE_TYPE", IMAGE_TYPE).strip())


def verify_keyring(ctx: Context, conf: RuntimeConf) -> None:
    data = ctx.root.read_bytes(KEYRING, 1024 * 1024)
    if not data:
        raise SysopsError("UNTRUSTED", f"The Waydroid repository key ({KEYRING}) is missing.")
    try:
        found = openpgp.primary_fingerprints(data)
    except openpgp.KeyringError as exc:
        raise SysopsError("UNTRUSTED", f"The Waydroid repository key cannot be read ({exc}).") from None
    if found != [conf.fingerprint]:
        raise SysopsError("UNTRUSTED", "The Waydroid repository key does not match the fingerprint Boswas pins; "
                                       "nothing was changed.", {"expected": conf.fingerprint, "found": found})


def binder_available(ctx: Context) -> bool:
    if ctx.root.exists("/dev/binder") or ctx.root.exists("/dev/binderfs"):
        return True
    if "binder" in (ctx.root.read("/proc/filesystems", 65536) or "").split():
        return True
    for base in ("/usr/lib/modules", "/lib/modules"):
        directory = f"{base}/{ctx.kernel_release}/kernel/drivers/android"
        if any(name.startswith(f"{MODULE}.ko") for name in ctx.root.listdir(directory)):
            return True
    return False


def sources_text(conf: RuntimeConf) -> str:
    return ("# Boswas OS: the Waydroid (Android runtime) repository.\n"
            "# Written by boswas-sysops android-runtime-install; removed by android-runtime-remove.\n"
            f"# Only the packages allowed in {PREF} are installed from it.\n"
            "Types: deb\n"
            f"URIs: {conf.repo_url}\n"
            f"Suites: {conf.suite}\n"
            f"Components: {' '.join(conf.components)}\n"
            f"Signed-By: {KEYRING}\n")


def pref_text(conf: RuntimeConf) -> str:
    # APT uses the first matching record for a package: the specific record
    # for the allowed packages must come before the catch-all.
    return ("# Boswas OS: from the Waydroid repository, only the Android runtime packages.\n"
            "# Written by boswas-sysops android-runtime-install.\n\n"
            "Explanation: Boswas: the Android runtime packages\n"
            f"Package: {' '.join(conf.allowed)}\n"
            f"Pin: origin \"{conf.host}\"\n"
            "Pin-Priority: 500\n\n"
            "Explanation: Boswas: nothing else from the Waydroid repository\n"
            "Package: *\n"
            f"Pin: origin \"{conf.host}\"\n"
            "Pin-Priority: -1\n")


MODPROBE_TEXT = ("# Boswas OS: Android runtime (Waydroid) needs the binder devices.\n"
                 f"options {MODULE} devices={BINDER_DEVICES}\n")
MODULES_LOAD_TEXT = "# Boswas OS: Android runtime (Waydroid).\n" f"{MODULE}\n"

# Waydroid's own desktop entries are hidden: its launcher and "Initialize
# Waydroid" dialog offer Android images with Google apps (never offered by
# Boswas OS), and its APK handler skips Boswas's APK inspection. The package's
# files are not modified: an entry with the same desktop ID in
# /usr/local/share/applications (earlier in XDG_DATA_DIRS) says Hidden=true.
OVERRIDE_DIR = "/usr/local/share/applications"
OVERRIDE_RECORD = "/var/lib/boswas/android/hidden-desktop-entries"
OVERRIDE_MARKER = "# boswas-sysops: hides the Waydroid entry of the same name"
DESKTOP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.desktop$")
GAPPS = re.compile(r"gapps", re.IGNORECASE)


def override_text(desktop_id: str) -> str:
    return (f"{OVERRIDE_MARKER}\n"
            "# (Boswas OS manages Android apps in Control Center; Google Play is not offered.)\n"
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={desktop_id[:-len('.desktop')]}\n"
            "NoDisplay=true\n"
            "Hidden=true\n")


def waydroid_desktop_ids(ctx: Context) -> list[str]:
    """Desktop entry file names shipped by the installed waydroid package (dpkg -L)."""
    result = ctx.runner.run(argv("dpkg-query", "-L", "waydroid"), TIMEOUT_QUERY)
    ids = []
    for line in result.stdout.splitlines() if result.code == 0 else []:
        path = line.strip()
        directory, _, name = path.rpartition("/")
        if directory == "/usr/share/applications" and DESKTOP_ID.fullmatch(name) and name not in ids:
            ids.append(name)
    return ids


def hide_waydroid_entries(ctx: Context) -> list[str]:
    """Write the hiding overrides; return their paths (also recorded for android-runtime-remove)."""
    written = []
    for desktop_id in waydroid_desktop_ids(ctx):
        path = f"{OVERRIDE_DIR}/{desktop_id}"
        existing = ctx.root.read(path, 64 * 1024)
        if existing is not None and OVERRIDE_MARKER not in existing:
            ctx.emit.log(f"{path} exists and was not written by Boswas; left unchanged", "warning")
            continue
        ctx.root.write_file(path, override_text(desktop_id), 0o644)
        written.append(path)
    ctx.root.write_file(OVERRIDE_RECORD, "".join(f"{p}\n" for p in written), 0o644)
    return written


def unhide_waydroid_entries(ctx: Context) -> list[str]:
    """Remove exactly the overrides android-runtime-install wrote (and still carry its marker)."""
    removed = []
    for path in (ctx.root.read(OVERRIDE_RECORD, 64 * 1024) or "").splitlines():
        directory, _, name = path.rpartition("/")
        if directory != OVERRIDE_DIR or not DESKTOP_ID.fullmatch(name):
            continue
        if OVERRIDE_MARKER in (ctx.root.read(path, 64 * 1024) or "") and ctx.root.remove_file(path):
            removed.append(path)
    ctx.root.remove_file(OVERRIDE_RECORD)
    return removed


def configured_gapps(ctx: Context) -> bool:
    """Whether Waydroid is configured for a Google-apps image (e.g. by Waydroid's own dialog)."""
    text = ctx.root.read(f"{DATA_DIR}/waydroid.cfg", 256 * 1024) or ""
    return any(GAPPS.search(line) for line in text.splitlines()
               if line.split("=", 1)[0].strip().lower() in ("system_ota", "vendor_ota", "system_type"))


def _installed(ctx: Context, packages) -> list[str]:
    # dpkg-query expands the \t and \n escape sequences of the format itself.
    result = ctx.runner.run(argv("dpkg-query", "-W", "-f", r"${Package}\t${db:Status-Status}\n", *packages),
                            TIMEOUT_QUERY)
    found = []
    for line in result.stdout.splitlines():
        name, _, state = line.partition("\t")
        if state.strip() == "installed":
            found.append(name)
    return found


def _require_waydroid(ctx: Context) -> None:
    if not ctx.root.executable(EXECUTABLES["waydroid"]):
        raise SysopsError("UNSUPPORTED", "The Android runtime is not installed. Install it first.")


def _systemctl(ctx: Context, *args: str) -> tuple[int, str]:
    result = ctx.runner.run(argv("systemctl", *args), TIMEOUT_SERVICE)
    return result.code, (result.stderr or result.stdout).strip()


def _modprobe(ctx: Context, *args: str) -> tuple[int, str]:
    result = ctx.runner.run(argv("modprobe", *args), TIMEOUT_QUERY)
    return result.code, (result.stderr or result.stdout).strip()


# --- operations --------------------------------------------------------------------------------------

def runtime_install(ctx: Context, args: list[str]) -> tuple[str, dict]:
    validate.no_arguments(args)
    policy.require_allowed(ctx.root, "ANDROID_RUNTIME", "The Android runtime")
    conf = load_conf(ctx)
    verify_keyring(ctx, conf)
    if not binder_available(ctx):
        raise SysopsError("UNSUPPORTED", "This kernel has no binder support, which the Android runtime needs.")
    locks.check(ctx.root, locks.FOR_REFRESH + locks.FOR_PACKAGES)
    files = ((SOURCES, sources_text(conf)), (PREF, pref_text(conf)), (MODPROBE, MODPROBE_TEXT),
             (MODULES_LOAD, MODULES_LOAD_TEXT))
    created = []
    try:
        ctx.emit.progress("configure", None, "Adding the Waydroid repository", cancellable=ctx.cancellable)
        for path, text in files:
            if not ctx.root.exists(path):
                created.append(path)
            ctx.root.write_file(path, text, 0o644)
        lines = apt.run_apt(ctx, "Refreshing the package lists", "update", download_phase="refresh",
                            timeout=apt.TIMEOUT_REFRESH)
        errors = [e for e in apt.refresh_errors(lines)[0] if conf.host in e]
        if errors:
            code = "UNTRUSTED" if any(t in " ".join(errors) for t in apt.UNTRUSTED_TEXT) else "NETWORK"
            raise SysopsError(code, "The Waydroid repository could not be reached or verified; nothing was "
                                    "installed.", {"errors": errors})
        plan = apt.simulate(ctx, "install", "waydroid")
        apt.check_plan(ctx, plan)
        apt.run_apt(ctx, "Installing the Android runtime", "-y", "install", "waydroid")
    except BaseException:
        for path in created:
            ctx.root.remove_file(path)
        raise
    code, detail = _modprobe(ctx, MODULE)
    if code != 0:
        raise SysopsError("FAILED", "The Android runtime is installed, but the binder module could not be loaded.",
                          {"detail": detail[-1000:]})
    code, detail = _systemctl(ctx, "disable", "--now", UNIT)
    if code != 0:
        ctx.emit.log(f"Could not turn off automatic start of {UNIT}: {detail[-300:]}", "warning")
    hidden = hide_waydroid_entries(ctx)
    return ("The Android runtime (Waydroid) is installed. Initialise it to download the Android system.",
            {"installed": apt.plan_items(plan), "repository": conf.repo_url, "hidden_desktop_entries": hidden})


def runtime_remove(ctx: Context, args: list[str]) -> tuple[str, dict]:
    purge = validate.remove_options(args) == ["--purge-data"]
    try:
        packages = load_conf(ctx).allowed
    except SysopsError:
        packages = DEFAULT_PACKAGES
    ctx.emit.progress("service", None, "Stopping the Android runtime", cancellable=False)
    if ctx.root.executable(EXECUTABLES["waydroid"]):
        _systemctl(ctx, "stop", UNIT)
    installed = _installed(ctx, packages)
    if installed:
        plan = apt.simulate(ctx, "remove", *installed)
        apt.check_plan(ctx, apt.Plan(install=[], remove=plan.remove))
        locks.check(ctx.root, locks.FOR_PACKAGES)
        apt.run_apt(ctx, "Removing the Android runtime", "-y", "remove", *installed)
    for path in (SOURCES, PREF, MODPROBE, MODULES_LOAD):
        ctx.root.remove_file(path)
    unhide_waydroid_entries(ctx)
    _modprobe(ctx, "-r", MODULE)
    purged = False
    if purge:
        target = ctx.root(DATA_DIR)
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            st = None
        if st is not None:
            if not stat.S_ISDIR(st.st_mode):
                raise SysopsError("FAILED", f"{DATA_DIR} is not a directory; it was left in place.")
            ctx.emit.progress("cleanup", None, "Removing the Android system images and data", cancellable=False)
            shutil.rmtree(target)
            purged = True
    message = "The Android runtime was removed."
    if purge:
        message += " Its system images and data were deleted." if purged else " There was no Android data to delete."
    return message, {"removed": installed, "purged": purged}


class WaydroidReporter(Reporter):
    """Waydroid's console output -> progress (download percentage, validation, extraction)."""

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self.lines: list[str] = []

    def line(self, raw: str) -> None:
        text = LOG_STAMP.sub("", raw).strip()
        if not text:
            return
        match = DOWNLOAD.search(text)
        if match:
            done, total = float(match.group(1)), float(match.group(2))
            if not self.changing:
                self.report("download", 100.0 * done / total if total > 0 else None, "Downloading the Android system")
            return
        self.lines.append(text)
        if text.startswith("Validating"):
            self.report("verify", None, text, force=True)
        elif text.startswith(("Extracting", "Installing")):
            self.changing_begins("install", None, text)
        elif text.startswith("Downloading"):
            self.report("download", 0.0, "Downloading the Android system", force=True)
        else:
            self.ctx.emit.log(text[:500], "info")


def _run_waydroid(ctx: Context, what: str, *args: str) -> list[str]:
    reporter = WaydroidReporter(ctx)
    result = ctx.runner.stream(argv("waydroid", *args), timeout=TIMEOUT_IMAGES, on_line=reporter.line,
                               poll=reporter.poll)
    if result.cancelled:
        raise SysopsError("CANCELLED", "Cancelled. The Android system was not changed.")
    if result.timed_out:
        raise SysopsError("FAILED", f"{what} did not finish in time and was stopped.", {"detail": result.detail})
    if result.code != 0:
        text = "\n".join(reporter.lines + result.tail)
        detail = {"detail": "\n".join((reporter.lines or result.tail)[-15:]), "exit_code": result.code}
        if "No space left on device" in text:
            raise SysopsError("DISK_SPACE", "There is not enough free disk space for the Android system.", detail)
        if any(t in text for t in CHECKSUM_TEXT):
            raise SysopsError("NETWORK", "The Android system download was incomplete or damaged, so its checksum did "
                                         "not match and it was not installed. Try again.", detail)
        if any(t in text for t in NETWORK_TEXT):
            raise SysopsError("NETWORK", "Downloading the Android system failed. Check the network connection and "
                                         "try again.", detail)
        raise SysopsError("FAILED", f"{what} failed (waydroid exit code {result.code}).", detail)
    return reporter.lines


def init(ctx: Context, args: list[str]) -> tuple[str, dict]:
    validate.no_arguments(args)
    policy.require_allowed(ctx.root, "ANDROID_RUNTIME", "The Android runtime")
    conf = load_conf(ctx)
    if conf.image_type != IMAGE_TYPE:
        raise SysopsError("NOT_ALLOWED", f"Only the {IMAGE_TYPE} Android system (without Google components) can be "
                                         "installed.")
    _require_waydroid(ctx)
    if configured_gapps(ctx):
        raise SysopsError("NOT_ALLOWED", "Waydroid is configured for an Android system with Google apps, which Boswas "
                                         "OS does not install. Remove the Android runtime with its data, then "
                                         "install and initialise it again.")
    free = ctx.root.free_bytes("/var/lib")
    if free is not None and free < MIN_FREE_FOR_IMAGES:
        raise SysopsError("DISK_SPACE", "The Android system needs at least 3 GB of free disk space.",
                          {"free_bytes": free})
    ctx.emit.progress("init", None, "Preparing the Android system", cancellable=ctx.cancellable)
    _run_waydroid(ctx, "Initialising the Android runtime", "init", "-s", IMAGE_TYPE)
    return "The Android system is installed. Start Android to use it.", {"image_type": IMAGE_TYPE}


def update(ctx: Context, args: list[str]) -> tuple[str, dict]:
    validate.no_arguments(args)
    _require_waydroid(ctx)
    ctx.emit.progress("download", None, "Checking for a newer Android system", cancellable=ctx.cancellable)
    _run_waydroid(ctx, "Updating the Android system", "upgrade")
    return "The Android system is up to date.", {}


def repair(ctx: Context, args: list[str]) -> tuple[str, dict]:
    validate.no_arguments(args)
    _require_waydroid(ctx)
    ctx.emit.progress("service", None, "Stopping the Android runtime", cancellable=False)
    _systemctl(ctx, "stop", UNIT)
    ctx.emit.progress("configure", None, "Re-applying the Android runtime configuration", cancellable=False)
    _run_waydroid(ctx, "Repairing the Android runtime", "upgrade", "--offline")
    code, detail = _modprobe(ctx, MODULE)
    if code != 0:
        raise SysopsError("FAILED", "The binder module could not be loaded.", {"detail": detail[-1000:]})
    ctx.emit.progress("service", None, "Starting the Android runtime", cancellable=False)
    code, detail = _systemctl(ctx, "start", UNIT)
    if code != 0:
        raise SysopsError("FAILED", "The Android runtime was repaired but could not be started.",
                          {"detail": detail[-1000:]})
    return "The Android runtime was repaired and started.", {}


SERVICE_WORDS = {"start": ("Starting", "started"), "stop": ("Stopping", "stopped"),
                 "restart": ("Restarting", "restarted")}


def service(ctx: Context, args: list[str]) -> tuple[str, dict]:
    verb = validate.service_verb(args)
    doing, done = SERVICE_WORDS[verb]
    _require_waydroid(ctx)
    ctx.emit.progress("service", None, f"{doing} the Android runtime", cancellable=False)
    code, detail = _systemctl(ctx, verb, UNIT)
    if code != 0:
        if "not found" in detail or "not loaded" in detail:
            raise SysopsError("UNSUPPORTED", "The Android runtime service is not installed.", {"detail": detail[-500:]})
        raise SysopsError("FAILED", f"The Android runtime could not be {done} (systemctl exit code {code}).",
                          {"detail": detail[-1000:]})
    return f"The Android runtime was {done}.", {"service": UNIT, "action": verb}
