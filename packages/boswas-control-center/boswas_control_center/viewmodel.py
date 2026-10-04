"""Presentation rules of Control Center (pure Python, no Qt, no I/O).

The widgets display what this module derives from the documents the backend
reads: the security states, the update, privacy, storage and power
summaries, the About rows, the application groups and the preset gallery.
Keeping the rules here makes them testable without a display, and keeps
decisions out of the GUI: Control Center explains what the system reports,
it never invents a check or a value.

Every string from outside (check details, application names, preset
descriptions, desktop entries, log lines) is untrusted and goes through
sanitize() or one_line() before it reaches a widget.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo

# --- fixed texts -------------------------------------------------------------------------------------

WINDOWS_SUPPORT = "x86_64 / 64-bit Windows applications only"
STORE_NOTE = ("Installing new software arrives with the Boswas Store in a later release of Boswas OS. "
              "Windows applications can already be installed with the Compatibility Manager.")
SESSION_UNAVAILABLE = ("The Boswas session service is not running, so Windows applications cannot be listed. "
                       "It starts automatically when you log in; log out and back in if this does not change.")
AGENT_UNAVAILABLE = "The Boswas device agent is not running, so device management information is not available."
LICENCES_TEXT = ("Boswas OS includes free and open-source software, for example from the Debian project and KDE. "
                 "Each package's copyright notice and licence are in /usr/share/doc/<package>/copyright on this "
                 "device. Boswas-authored components are provided by Boswas Group under their own licence.")
INSTALL_POINTS = (
    "The live session is temporary: files you create and settings you change are lost when you shut down.",
    "Nothing is written to this computer's internal disks unless you choose to install Boswas OS.",
    "Installation runs as a separate boot mode. Restart the computer and choose “Install Boswas OS” in the "
    "boot menu.",
)
INSTALL_BOOT_HINT = "When the boot menu appears, choose “Install Boswas OS”."
UPDATES_AUTOMATIC = ("Security updates are downloaded and installed automatically in the background. "
                     "This release has no manual updater; there is nothing you need to do.")
UPDATES_OFF = ("Automatic security updates are turned off on this device. Contact your administrator: "
               "this release has no manual updater.")
NEVER_COLLECTED = ("Boswas OS never collects keystrokes, screen contents, your files or browsing history, the "
                   "commands you type, or which applications you start.")

# --- untrusted text ------------------------------------------------------------------------------------

_BIDI_CONTROLS = "".join(map(chr, (*range(0x202A, 0x202F), *range(0x2066, 0x206A))))
_CONTROL_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
    r"|\x1b[PX^_][^\x1b\x9c]*(?:\x1b\\|\x9c)?"
    r"|\x1b[ -/]*[0-~]?"
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"
    rf"|[{_BIDI_CONTROLS}]"
)
_HOME_RE = re.compile(r"/home/[^/\s]+")


def sanitize(value: object) -> str:
    """Text without escape sequences or control characters (newline and tab stay)."""
    if value is None:
        return ""
    return _CONTROL_RE.sub("", value if isinstance(value, str) else str(value))


def one_line(value: object, limit: int = 200) -> str:
    """A sanitised single line, whitespace collapsed, shortened with an ellipsis."""
    text = " ".join(sanitize(value).split())
    return text if len(text) <= limit else text[:max(1, limit - 1)].rstrip() + "…"


def sentence(value: object, limit: int = 400) -> str:
    """one_line() with /home/<user> shortened to ~ and the first word capitalised when it is a plain word
    (names such as nftables.service or mode=allow keep their spelling)."""
    text = _HOME_RE.sub("~", one_line(value, limit))
    first = text.split(" ", 1)[0].rstrip(":;,")
    return text[:1].upper() + text[1:] if first.isalpha() and first.islower() else text


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def format_bytes(value: object) -> str:
    size = _number(value)
    if size is None or size < 0:
        return "Unknown"
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return "Unknown"


def format_epoch(value: object, tz: tzinfo | None = None) -> str:
    seconds = _number(value)
    if seconds is None or seconds <= 0:
        return ""
    return datetime.fromtimestamp(seconds, timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M")


def format_iso(value: object, tz: tzinfo | None = None) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return one_line(value, 40)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


# --- KEY="value" files (release, image-info, update.conf, device.conf) -----------------------------

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_env(text: object) -> dict[str, str]:
    """Shell-style KEY="value" lines, read as data: quotes are removed, nothing is evaluated or expanded."""
    result: dict[str, str] = {}
    if not isinstance(text, str):
        return result
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            continue
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError:
            continue
        result[key] = " ".join(parts)
    return result


@dataclass(frozen=True)
class ReleaseInfo:
    name: str
    version: str
    version_id: str
    channel: str
    build_id: str
    build_date: str
    git_commit: str
    debian_version: str

    @property
    def version_text(self) -> str:
        if self.version and self.version_id and self.version != self.version_id:
            return f"{self.version} ({self.version_id})"
        return self.version or self.version_id or "Unknown"


def release_info(doc: object) -> ReleaseInfo:
    """doc: {"release": {...}, "image": {...}, "update": {...}, "debian_version": str} (probes.release())."""
    doc = _dict(doc)
    release, image, update = _dict(doc.get("release")), _dict(doc.get("image")), _dict(doc.get("update"))

    def pick(*values: object, limit: int = 80) -> str:
        return next((one_line(v, limit) for v in values if isinstance(v, str) and v.strip()), "")

    return ReleaseInfo(
        name=pick(release.get("BOSWAS_NAME"), image.get("BOSWAS_NAME")) or "Boswas OS",
        version=pick(release.get("BOSWAS_VERSION"), image.get("BOSWAS_VERSION")),
        version_id=pick(release.get("BOSWAS_VERSION_ID"), image.get("BOSWAS_VERSION_ID")),
        channel=pick(update.get("CHANNEL"), release.get("BOSWAS_CHANNEL"), image.get("BOSWAS_CHANNEL"), limit=20),
        build_id=pick(image.get("BOSWAS_BUILD_ID")),
        build_date=format_iso(image.get("BOSWAS_BUILD_DATE")),
        git_commit=pick(image.get("BOSWAS_GIT_COMMIT"), limit=80)[:12],
        debian_version=pick(doc.get("debian_version"), limit=20),
    )


# --- live session --------------------------------------------------------------------------------------

def is_live(medium_present: bool, cmdline: object, session_marker: object = None) -> bool:
    """A live session: the live medium is mounted, the kernel was booted with boot=live, or
    /run/boswas/session (written by the live system at boot) says "live"."""
    tokens = cmdline.split() if isinstance(cmdline, str) else []
    marker = session_marker.split() if isinstance(session_marker, str) else []
    return bool(medium_present) or "boot=live" in tokens or "live" in marker


# --- hardware --------------------------------------------------------------------------------------------

def parse_cpuinfo(text: object) -> tuple[str, int]:
    """(model name, number of logical processors) from /proc/cpuinfo."""
    model, count = "", 0
    for line in text.splitlines() if isinstance(text, str) else []:
        key, _, value = line.partition(":")
        key = key.strip()
        if key == "processor":
            count += 1
        elif key == "model name" and not model:
            model = one_line(value, 120)
    return model, count


def parse_meminfo(text: object) -> int | None:
    """MemTotal in bytes from /proc/meminfo."""
    for line in text.splitlines() if isinstance(text, str) else []:
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


_METAINFO_RE = re.compile(r'<release\s[^>]*version="([0-9][0-9.]*)"')
_PLASMASHELL_RE = re.compile(r"plasmashell\s+([0-9][0-9.]*)")


def plasma_version_from_metainfo(text: object) -> str | None:
    """The newest release listed in org.kde.plasmashell.metainfo.xml (releases are newest first)."""
    match = _METAINFO_RE.search(text) if isinstance(text, str) else None
    return match.group(1) if match else None


def plasma_version_from_output(text: object) -> str | None:
    match = _PLASMASHELL_RE.search(text) if isinstance(text, str) else None
    return match.group(1) if match else None


def cpu_text(hardware: object) -> str:
    hw = _dict(hardware)
    model = one_line(hw.get("cpu_model"), 120)
    threads = hw.get("cpu_threads")
    if model and isinstance(threads, int) and threads > 0:
        return f"{model} ({_plural(threads, 'thread')})"
    return model or "Unknown"


# --- status states ----------------------------------------------------------------------------------------

SECURE, WARNING, ATTENTION, ERROR, INFO, UNAVAILABLE, CHECKING = (
    "secure", "warning", "attention", "error", "info", "unavailable", "checking")
STATE_LABELS = {SECURE: "Secure", WARNING: "Warning", ATTENTION: "Attention", ERROR: "Error", INFO: "Info",
                UNAVAILABLE: "Not available", CHECKING: "Checking…"}
# How the boswas CLI's check statuses are shown.
CHECK_STATES = {"PASS": SECURE, "WARN": WARNING, "FAIL": ATTENTION, "INFO": INFO, "UNKNOWN": UNAVAILABLE}
# Phrases of the CLI meant for a terminal, reworded for the window.
_DETAIL_REWORDING = {"run as root to check": "Only an administrator can check this"}


def check_detail(value: object) -> str:
    text = one_line(value, 400)
    for phrase, replacement in _DETAIL_REWORDING.items():
        if text.casefold() == phrase:
            return replacement
    return sentence(text)


def check_state(check: object) -> str:
    """The display state of one check of `boswas --json status`."""
    check = _dict(check)
    status = check.get("status")
    if status == "UNKNOWN" and one_line(check.get("detail")).startswith("check failed"):
        return ERROR                          # the probe itself crashed
    return CHECK_STATES.get(status, ERROR)


@dataclass(frozen=True)
class SecurityRow:
    key: str
    title: str
    state: str
    detail: str

    @property
    def label(self) -> str:
        return STATE_LABELS[self.state]


@dataclass(frozen=True)
class Summary:
    state: str
    headline: str
    detail: str


@dataclass(frozen=True)
class SecurityView:
    rows: list[SecurityRow]
    other_rows: list[SecurityRow]
    summary: Summary
    basis: str = ""


# (row key, title, check ID of `boswas --json status`)
SECURITY_CHECK_ROWS = (
    ("apparmor", "AppArmor", "apparmor"),
    ("winapp", "Windows app sandbox", "winapp-confinement"),
    ("firewall", "Firewall", "firewall"),
    ("secure-boot", "Secure Boot", "secure-boot"),
    ("disk-encryption", "Disk encryption", "disk-encryption"),
    ("screen-lock", "Screen lock", "screen-lock"),
    ("updates", "Automatic security updates", "updates"),
)
SECURITY_ROW_KEYS = tuple(k for k, _t, _c in SECURITY_CHECK_ROWS) + ("agent", "policy")
# Checks represented by the device agent rows instead of their own row.
_AGENT_CHECK_IDS = frozenset({"agent", "enrollment"})

DEVICE_STATE_LABELS = {"INITIALIZING": "Starting", "READY": "Ready", "DEGRADED": "Degraded", "OFFLINE": "Offline",
                       "UPDATING": "Updating", "ERROR": "Error", "MAINTENANCE": "Maintenance"}
DEVICE_STATE_STATES = {"INITIALIZING": INFO, "READY": SECURE, "DEGRADED": WARNING, "OFFLINE": WARNING,
                       "UPDATING": INFO, "ERROR": ERROR, "MAINTENANCE": WARNING}
CONNECTION_LABELS = {"CONNECTED": "Connected", "OFFLINE": "Offline", "STANDALONE": "Standalone",
                     "UNENROLLED": "Not enrolled", "REVOKED": "Revoked"}
ENROLLMENT_LABELS = {"enrolled": "Enrolled", "unenrolled": "Not enrolled", "pending": "Enrollment pending",
                     "retired": "Retired"}
APPARMOR_MODE_LABELS = {"enforce": "Enforcing", "complain": "Complain mode (not enforcing)", "not-loaded": "Not loaded",
                        "unavailable": "AppArmor not available", "unknown": "Unknown"}


def agent_running(agent: object) -> bool:
    return isinstance(agent, dict) and agent.get("available") is not False and "state" in agent


def _checks_by_id(status: object) -> dict[str, dict]:
    checks = _dict(status).get("checks")
    if not isinstance(checks, list):
        return {}
    return {c["id"]: c for c in checks if isinstance(c, dict) and isinstance(c.get("id"), str)}


def _check_row(key: str, title: str, check: dict | None, status_error: str | None, loading: bool) -> SecurityRow:
    if check is not None:
        return SecurityRow(key, title, check_state(check), check_detail(check.get("detail")) or "No details reported.")
    if status_error:
        return SecurityRow(key, title, ERROR, f"The security status could not be read: {one_line(status_error, 300)}")
    if loading:
        return SecurityRow(key, title, CHECKING, "Checking…")
    return SecurityRow(key, title, UNAVAILABLE, "This check is not reported by the boswas tool on this device.")


def winapp_row(check: dict | None, runtime: object, status_error: str | None = None,
               loading: bool = False) -> SecurityRow:
    """Windows app sandbox: the CLI check when it is conclusive, else what the device agent's runtime check saw.

    As a normal user the CLI cannot read the AppArmor profile state ("run as
    root to check"); the device agent runs the same runtime check as root and
    reports the boswas-winapp profile mode through its read-only API.
    """
    key, title = "winapp", "Windows app sandbox"
    if check is not None and check.get("status") in ("PASS", "WARN", "FAIL"):
        return SecurityRow(key, title, check_state(check), check_detail(check.get("detail")))
    if check is not None and check.get("status") == "INFO":
        return SecurityRow(key, title, INFO, check_detail(check.get("detail")))
    rt = _dict(runtime)
    mode = _dict(rt.get("apparmor")).get("mode")
    if mode in APPARMOR_MODE_LABELS and mode != "unknown":
        bubblewrap = _dict(rt.get("bubblewrap"))
        if mode == "enforce" and bubblewrap.get("available") is False:
            return SecurityRow(key, title, ATTENTION, "The bubblewrap sandbox is missing, so Windows applications "
                                                      "cannot start.")
        if mode == "enforce":
            return SecurityRow(key, title, SECURE, "Windows applications run in a bubblewrap sandbox, confined by "
                                                   "the boswas-winapp AppArmor profile (enforcing).")
        if mode == "complain":
            return SecurityRow(key, title, WARNING, "The boswas-winapp AppArmor profile is in complain mode, so it "
                                                    "does not enforce confinement.")
        return SecurityRow(key, title, WARNING, f"Windows app confinement: {APPARMOR_MODE_LABELS[mode]}. Windows "
                                                "applications will not start until it is enforcing.")
    if check is not None:
        return SecurityRow(key, title, check_state(check), check_detail(check.get("detail")))
    return _check_row(key, title, None, status_error, loading)


def agent_row(agent: object, agent_error: str | None, loading: bool = False) -> SecurityRow:
    key, title = "agent", "Device agent"
    if not agent_running(agent):
        if agent_error:
            return SecurityRow(key, title, ATTENTION, AGENT_UNAVAILABLE)
        if loading or agent is None:
            return SecurityRow(key, title, CHECKING, "Checking…")
        return SecurityRow(key, title, ATTENTION, AGENT_UNAVAILABLE)
    state = agent.get("state")
    connection = agent.get("connection")
    parts = [f"Running: {DEVICE_STATE_LABELS.get(state, one_line(state, 20).capitalize() or 'Unknown')}",
             f"Control Plane: {CONNECTION_LABELS.get(connection, 'Unknown')}",
             ENROLLMENT_LABELS.get(agent.get("enrollment"), "Enrollment unknown")]
    reasons = [one_line(r, 120) for r in agent.get("reasons") or [] if isinstance(r, str)]
    detail = " · ".join(parts) + (f" ({'; '.join(reasons[:3])})" if reasons else "")
    level = DEVICE_STATE_STATES.get(state, UNAVAILABLE)
    if connection == "REVOKED":
        level = ATTENTION
    return SecurityRow(key, title, level, detail)


def policy_row(agent: object, agent_error: str | None, loading: bool = False) -> SecurityRow:
    key, title = "policy", "Device policy"
    if not agent_running(agent):
        if loading and not agent_error:
            return SecurityRow(key, title, CHECKING, "Checking…")
        return SecurityRow(key, title, UNAVAILABLE, "The device agent is not running, so the policy state is unknown.")
    policy = _dict(agent.get("policy"))
    if policy.get("managed") is True:
        version = one_line(policy.get("version"), 40) or "unknown version"
        applied = format_iso(policy.get("applied_at"))
        detail = f"Managed policy {version} from your organisation" + (f", applied {applied}." if applied else ".")
        return SecurityRow(key, title, SECURE, detail)
    return SecurityRow(key, title, INFO, "Local policy: this device is not managed by an organisation. The Boswas "
                                         "security baseline applies.")


def security_summary(rows: list[SecurityRow]) -> Summary:
    counts = {state: sum(1 for r in rows if r.state == state) for state in STATE_LABELS}
    if counts[CHECKING] and counts[CHECKING] == len(rows):
        return Summary(CHECKING, "Checking your device…", "The security checks are running on this device.")
    if counts[ATTENTION]:
        return Summary(ATTENTION, f"{_plural(counts[ATTENTION], 'item needs', 'items need')} your attention",
                       "Review the items marked Attention below.")
    if counts[ERROR]:
        return Summary(ERROR, "Some security information could not be read",
                       "The items marked Error below could not be checked.")
    if counts[WARNING]:
        return Summary(WARNING, f"Protected, with {_plural(counts[WARNING], 'item')} to review",
                       "The items marked Warning below are weaker than the Boswas OS baseline.")
    if counts[SECURE]:
        return Summary(SECURE, "Your device is protected", "Every protection that could be checked is active.")
    return Summary(UNAVAILABLE, "Security status not available", "No security check could be read.")


def security_view(status: object, status_error: str | None, agent: object, agent_error: str | None,
                  runtime: object = None, loading: bool = False, agent_loading: bool = False) -> SecurityView:
    """The Security Center from `boswas --json status`, the device agent's status and its runtime check."""
    checks = _checks_by_id(status)
    rows = []
    for key, title, check_id in SECURITY_CHECK_ROWS:
        check = checks.get(check_id)
        if key == "winapp":
            rows.append(winapp_row(check, runtime, status_error, loading))
        else:
            rows.append(_check_row(key, title, check, status_error, loading))
    rows.append(agent_row(agent, agent_error, agent_loading))
    rows.append(policy_row(agent, agent_error, agent_loading))
    shown = {check_id for _k, _t, check_id in SECURITY_CHECK_ROWS} | _AGENT_CHECK_IDS
    other = [SecurityRow(f"check:{cid}", one_line(c.get("title"), 60) or cid, check_state(c),
                         check_detail(c.get("detail")) or "No details reported.")
             for cid, c in checks.items() if cid not in shown]
    basis = sentence(_dict(_dict(status).get("compliance")).get("basis"), 300)
    return SecurityView(rows, other, security_summary(rows + other), basis)


def security_line(view: SecurityView | None) -> str:
    """One line for the About page."""
    if view is None:
        return "Not checked yet"
    rows = view.rows + view.other_rows
    secure = sum(1 for r in rows if r.state == SECURE)
    review = sum(1 for r in rows if r.state in (WARNING, ATTENTION, ERROR))
    if view.summary.state == CHECKING:
        return "Checking…"
    if not secure and not review:
        return view.summary.headline
    return f"{_plural(secure, 'protection')} active, {_plural(review, 'item')} to review"


# --- device agent -------------------------------------------------------------------------------------------

def agent_rows(agent: object, agent_error: str | None) -> list[tuple[str, str]]:
    """Device management summary (System page)."""
    if not agent_running(agent):
        return [("Device agent", "Not running" if agent_error else "Checking…")]
    policy = _dict(agent.get("policy"))
    version = one_line(policy.get("version"), 40)
    plane = one_line(agent.get("control_plane"), 120)
    return [
        ("Device agent", f"Running ({DEVICE_STATE_LABELS.get(agent.get('state'), 'Unknown')}), version "
                         f"{one_line(agent.get('agent_version'), 30) or 'unknown'}"),
        ("Control Plane", CONNECTION_LABELS.get(agent.get("connection"), "Unknown") + (f" ({plane})" if plane else "")),
        ("Enrollment", ENROLLMENT_LABELS.get(agent.get("enrollment"), "Unknown")),
        ("Policy", f"Managed ({version})" if policy.get("managed") is True and version
         else ("Managed" if policy.get("managed") is True else "Local")),
        ("Device ID", one_line(agent.get("device_id"), 40) or "Not assigned"),
    ]


def agent_line(agent: object, agent_error: str | None) -> str:
    if not agent_running(agent):
        return "Not running" if agent_error else "Checking…"
    return (f"Running ({DEVICE_STATE_LABELS.get(agent.get('state'), 'Unknown')}), Control Plane: "
            f"{CONNECTION_LABELS.get(agent.get('connection'), 'Unknown')}")


# --- Windows compatibility --------------------------------------------------------------------------------

WINDOWS_APP_STATES = {"INSTALLING": "Installing", "INSTALLED": "Installed", "RUNNING": "Running",
                      "STOPPED": "Stopped", "ERROR": "Error", "REPAIR_REQUIRED": "Repair required",
                      "BLOCKED": "Blocked", "UNSUPPORTED": "Unsupported"}


@dataclass(frozen=True)
class WindowsView:
    summary: Summary
    rows: list[tuple[str, str]]
    app_count: int | None


def windows_view(system: object, system_error: str | None, apps: object, apps_error: str | None,
                 agent: object = None, loading: bool = False) -> WindowsView:
    count = len([a for a in apps if isinstance(a, dict)]) if isinstance(apps, list) else None
    if not isinstance(system, dict):
        healthy = _dict(agent).get("runtime_healthy") if agent_running(agent) else None
        if loading and not system_error:
            summary = Summary(CHECKING, "Checking the Windows compatibility runtime…", "")
        elif healthy is True:
            summary = Summary(SECURE, "The runtime is healthy",
                              "Reported by the device agent. " + SESSION_UNAVAILABLE)
        elif healthy is False:
            summary = Summary(WARNING, "The runtime needs attention",
                              "Reported by the device agent. " + SESSION_UNAVAILABLE)
        else:
            summary = Summary(UNAVAILABLE, "Windows compatibility status is not available",
                              SESSION_UNAVAILABLE if system_error else "")
        rows = [("Supported applications", WINDOWS_SUPPORT),
                ("Installed applications", str(count) if count is not None else "Not available")]
        return WindowsView(summary, rows, count)
    runtime = _dict(system.get("runtime"))
    wine = _dict(runtime.get("wine"))
    bubblewrap = _dict(runtime.get("bubblewrap"))
    mode = _dict(runtime.get("apparmor")).get("mode")
    policy = _dict(runtime.get("policy"))
    healthy = runtime.get("healthy") is True
    if healthy:
        summary = Summary(SECURE, "The runtime is healthy",
                          "Windows applications run in their own sandbox, confined by AppArmor.")
    else:
        problem = one_line(runtime.get("error"), 200)
        summary = Summary(WARNING, "The runtime needs attention",
                          sentence(problem) if problem else "Open the Compatibility Manager for details.")
    rows = [
        ("Supported applications", WINDOWS_SUPPORT),
        ("Installed applications", str(count) if count is not None else
         ("Not available" if apps_error else "Checking…")),
        ("Wine", one_line(wine.get("version"), 60) or "Available" if wine.get("available") is True else "Missing"),
        ("Sandbox", f"Bubblewrap {one_line(bubblewrap.get('version'), 20)}".strip()
         if bubblewrap.get("available") is True else "Bubblewrap missing"),
        ("Confinement", APPARMOR_MODE_LABELS.get(mode, "Unknown")),
        ("Policy", "Managed by your organisation" if policy.get("managed") is True else "Local"),
    ]
    return WindowsView(summary, rows, count)


def windows_line(view: WindowsView | None) -> str:
    if view is None:
        return "Not checked yet"
    if view.summary.state == SECURE:
        apps = "" if view.app_count is None else f", {_plural(view.app_count, 'application')} installed"
        return f"Runtime healthy{apps} (x86_64, 64-bit only)"
    return view.summary.headline


# --- updates -----------------------------------------------------------------------------------------------

APT_SHELL_VARS = (("UU", "Unattended-Upgrade"), ("PL", "Update-Package-Lists"),
                  ("DL", "Download-Upgradeable-Packages"), ("AC", "AutocleanInterval"))
_APT_SHELL_RE = re.compile(r"^([A-Z]{2})='([^']*)'$")
_APT_CONF_RE = re.compile(r'APT::Periodic::([A-Za-z-]+)\s+"([^"]*)"\s*;')
UPDATE_POLICY_LABELS = {"manual": "Manual (updates are not installed automatically)",
                        "security-only": "Security updates only", "managed": "Managed by your organisation"}
UU_LOG_MARKERS = ("Packages that will be upgraded", "All upgrades installed", "No packages found that can be upgraded",
                  "Installing the upgrades failed", "upgrade result")


def parse_apt_shell(text: object) -> dict[str, str]:
    """`apt-config shell UU APT::Periodic::Unattended-Upgrade ...` output -> {"Unattended-Upgrade": "1", ...}."""
    names = dict(APT_SHELL_VARS)
    result: dict[str, str] = {}
    for line in text.splitlines() if isinstance(text, str) else []:
        match = _APT_SHELL_RE.match(line.strip())
        if match and match.group(1) in names:
            result[names[match.group(1)]] = match.group(2)
    return result


def parse_apt_conf(texts: object) -> dict[str, str]:
    """APT::Periodic values from apt.conf.d fragments in file-name order (a later file overrides)."""
    result: dict[str, str] = {}
    for text in texts if isinstance(texts, (list, tuple)) else []:
        if isinstance(text, str):
            for match in _APT_CONF_RE.finditer(text):
                result[match.group(1)] = match.group(2)
    return result


def _days(value: object) -> int | None:
    text = str(value).strip() if value is not None else ""
    return int(text) if text.isdigit() else None


def _every(days: int | None) -> str:
    if days is None:
        return "Unknown"
    if days == 0:
        return "Never (turned off)"
    return "Every day" if days == 1 else f"Every {days} days"


@dataclass(frozen=True)
class UpdatesView:
    summary: Summary
    rows: list[tuple[str, str]]
    log_lines: list[str]
    log_note: str


def updates_view(doc: object, update_policy: str | None = None, live: bool = False,
                 tz: tzinfo | None = None) -> UpdatesView:
    """doc: backend.updates() -> {"periodic", "source", "stamps", "log", "channel", "repository"}."""
    doc = _dict(doc)
    periodic = _dict(doc.get("periodic"))
    stamps = _dict(doc.get("stamps"))
    unattended = _days(periodic.get("Unattended-Upgrade"))
    refresh = _days(periodic.get("Update-Package-Lists"))
    automatic = None if unattended is None and not periodic else bool(unattended)
    if automatic is None:
        summary = Summary(UNAVAILABLE, "The update settings could not be read",
                          "APT's periodic settings are not readable on this device.")
    elif automatic:
        summary = Summary(SECURE, "Automatic security updates are on", UPDATES_AUTOMATIC)
    else:
        summary = Summary(WARNING, "Automatic security updates are off", UPDATES_OFF)
    last_refresh = format_epoch(stamps.get("update-success-stamp") or stamps.get("update-stamp"), tz)
    last_run = format_epoch(stamps.get("unattended-upgrades-stamp") or stamps.get("upgrade-stamp"), tz)
    repository = one_line(doc.get("repository"), 120)
    rows = [
        ("Automatic security updates", "On (unattended-upgrades)" if automatic else
         ("Off" if automatic is False else "Unknown")),
        ("Package lists refreshed", _every(refresh)),
        ("Last refresh", last_refresh or "Not yet on this device"),
        ("Last automatic update run", last_run or "Not yet on this device"),
        ("Update source", repository or "Debian security updates (the Boswas update repository is not yet "
                                        "provisioned)"),
        ("Update channel", one_line(doc.get("channel"), 20) or "Unknown"),
    ]
    if update_policy:
        rows.append(("Device update policy", UPDATE_POLICY_LABELS.get(update_policy, one_line(update_policy, 40))))
    log = _dict(doc.get("log"))
    lines = [one_line(line, 300) for line in log.get("lines") or [] if isinstance(line, str)]
    relevant = [line for line in lines if any(marker in line for marker in UU_LOG_MARKERS)][-6:]
    if log.get("readable") is True:
        note = "" if relevant else "The update log has no completed runs yet."
    else:
        note = "The update log (/var/log/unattended-upgrades) is readable by administrators only."
    if live:
        note = (note + " " if note else "") + "In a live session, updates are lost when you shut down."
    return UpdatesView(summary, rows, relevant, note)


# --- privacy ---------------------------------------------------------------------------------------------

INVENTORY_LABELS = {
    "off": "Off: no inventory is sent",
    "minimal": "Minimal: OS and Boswas package versions, Windows application counts and runtime health",
    "standard": "Standard: the minimal inventory plus CPU, memory, model, firmware version and disk size",
}
TELEMETRY_LABELS = {
    "none": "None: no security status is sent",
    "security": "Security: posture check results only (for example “firewall: PASS”)",
}
PRIVACY_SETTINGS = ("INVENTORY_POLICY", "TELEMETRY_POLICY", "UPDATE_POLICY")


@dataclass(frozen=True)
class PrivacyView:
    summary: Summary
    rows: list[tuple[str, str]]


def device_settings(config: object) -> dict[str, str]:
    """The allowlisted privacy and update settings of device.config (nothing else is displayed)."""
    settings = _dict(_dict(config).get("settings"))
    return {key: one_line(settings.get(key), 40) for key in PRIVACY_SETTINGS if isinstance(settings.get(key), str)}


def privacy_view(agent: object, config: object, agent_error: str | None = None) -> PrivacyView:
    settings = device_settings(config)
    enrolled = agent_running(agent) and agent.get("enrollment") == "enrolled"
    if enrolled:
        plane = one_line(agent.get("control_plane"), 120)
        summary = Summary(INFO, "This device is managed by your organisation",
                          "The Boswas device agent sends your organisation's Control Plane"
                          + (f" ({plane})" if plane else "") + " only the information listed below.")
    elif agent_running(agent):
        summary = Summary(SECURE, "Nothing leaves this device",
                          "This device is not enrolled with a Control Plane, so the device agent sends nothing. "
                          "The settings below would apply if your organisation enrolled it.")
    elif agent_error:
        summary = Summary(UNAVAILABLE, "Device management status not available",
                          AGENT_UNAVAILABLE + " The settings below are read from the device configuration.")
    else:
        summary = Summary(CHECKING, "Checking…", "")
    inventory = settings.get("INVENTORY_POLICY")
    telemetry = settings.get("TELEMETRY_POLICY")
    rows = [
        ("Device management", (f"Enrolled ({CONNECTION_LABELS.get(agent.get('connection'), 'Unknown')})"
                               if enrolled else "Not enrolled: nothing is sent")
         if agent_running(agent) else "Unknown"),
        ("Inventory", INVENTORY_LABELS.get(inventory, inventory or "Unknown")),
        ("Security status reports", TELEMETRY_LABELS.get(telemetry, telemetry or "Unknown")),
        ("Never collected", NEVER_COLLECTED),
    ]
    return PrivacyView(summary, rows)


# --- storage and power -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class StorageRow:
    key: str
    title: str
    path: str
    total: int
    free: int
    used: int
    percent_used: int
    text: str
    state: str


def storage_rows(entries: object) -> list[StorageRow]:
    rows = []
    for entry in entries if isinstance(entries, list) else []:
        entry = _dict(entry)
        total, free = _number(entry.get("total")), _number(entry.get("free"))
        if not total or free is None or total <= 0:
            continue
        free = max(0.0, min(free, total))
        used = total - free
        percent = int(round(used * 100 / total))
        low = free < total * 0.05 or free < 2 * 1024 ** 3
        rows.append(StorageRow(
            key=one_line(entry.get("key"), 20), title=one_line(entry.get("title"), 60) or "Disk",
            path=one_line(entry.get("path"), 120), total=int(total), free=int(free), used=int(used),
            percent_used=percent,
            text=f"{format_bytes(used)} used of {format_bytes(total)} ({format_bytes(free)} free)",
            state=WARNING if low else SECURE))
    return rows


def storage_line(entries: object) -> str:
    rows = storage_rows(entries)
    system = next((r for r in rows if r.key == "system"), rows[0] if rows else None)
    return f"{format_bytes(system.total)} ({format_bytes(system.free)} free)" if system else "Unknown"


BATTERY_STATUS = {"Charging": "Charging", "Discharging": "On battery", "Full": "Fully charged",
                  "Not charging": "Plugged in, not charging", "Unknown": "Status unknown"}


@dataclass(frozen=True)
class BatteryRow:
    name: str
    percent: int | None
    status: str
    state: str


@dataclass(frozen=True)
class PowerView:
    batteries: list[BatteryRow]
    on_mains: bool | None
    summary: str


def power_view(supplies: object) -> PowerView:
    batteries, mains = [], []
    for supply in supplies if isinstance(supplies, list) else []:
        supply = _dict(supply)
        kind = supply.get("type")
        if kind == "Battery" and supply.get("scope") != "Device":
            capacity = supply.get("capacity")
            percent = int(capacity) if isinstance(capacity, str) and capacity.isdigit() else None
            status = BATTERY_STATUS.get(supply.get("status"), one_line(supply.get("status"), 30) or "Status unknown")
            state = WARNING if percent is not None and percent <= 15 and supply.get("status") == "Discharging" \
                else SECURE
            name = one_line(supply.get("model_name"), 60) or one_line(supply.get("name"), 30) or "Battery"
            batteries.append(BatteryRow(name, percent, status, state))
        elif kind in ("Mains", "USB"):
            mains.append(supply.get("online") == "1")
    on_mains = any(mains) if mains else None
    if batteries:
        first = batteries[0]
        level = f"{first.percent}%" if first.percent is not None else "unknown charge"
        summary = f"Battery at {level}, {first.status.lower()}"
    elif on_mains is not None:
        summary = "No battery detected. This device runs on mains power."
    else:
        summary = "No battery or power adapter information is available on this device."
    return PowerView(batteries, on_mains, summary)


# --- About ---------------------------------------------------------------------------------------------------

def about_rows(release: ReleaseInfo, hardware: object, storage: object, plasma: object, live: object,
               security: SecurityView | None, windows: WindowsView | None, agent: object,
               agent_error: str | None) -> list[tuple[str, str]]:
    hw = _dict(hardware)
    memory = hw.get("memory_bytes")
    return [
        ("Version", release.version_text),
        ("Build", release.build_id or "Unknown"),
        ("Architecture", one_line(hw.get("machine"), 20) or "Unknown"),
        ("Kernel", one_line(hw.get("kernel"), 80) or "Unknown"),
        ("Processor", cpu_text(hw)),
        ("Memory", format_bytes(memory) if memory else "Unknown"),
        ("Storage", storage_line(storage)),
        ("Desktop", f"KDE Plasma {one_line(plasma, 20)}" if isinstance(plasma, str) and plasma else "Not available"),
        ("Session", ("Live session" if live else "Installed") if isinstance(live, bool) else "Checking…"),
        ("Security", security_line(security)),
        ("Windows compatibility", windows_line(windows)),
        ("Device agent", agent_line(agent, agent_error)),
    ]


def technical_rows(release: ReleaseInfo, hardware: object) -> list[tuple[str, str]]:
    hw = _dict(hardware)
    rows = [("Package base", f"Debian {release.debian_version}" if release.debian_version else "Debian")]
    if release.version_id:
        rows.append(("Version ID", release.version_id))
    if release.channel:
        rows.append(("Update channel", release.channel))
    if release.build_date:
        rows.append(("Build date", release.build_date))
    if release.git_commit:
        rows.append(("Source revision", release.git_commit))
    rows.append(("Boot mode", one_line(hw.get("boot_mode"), 10) or "Unknown"))
    if hw.get("hostname"):
        rows.append(("Device name", one_line(hw.get("hostname"), 64)))
    return rows


def about_text(name: str, rows: list[tuple[str, str]], technical: list[tuple[str, str]]) -> str:
    """Plain text for the clipboard (support requests)."""
    lines = [name] + [f"{label}: {value}" for label, value in rows + technical]
    return "\n".join(lines) + "\n"


# --- desktop entries and the application groups -----------------------------------------------------------

_DESKTOP_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+-]{0,200}$")
_GROUP_RE = re.compile(r"^\[(.+)\]$")
_ESCAPES = {"s": " ", "n": " ", "t": " ", "r": "", "\\": "\\", ";": ";"}
OWN_DESKTOP_IDS = frozenset({"com.boswas.ControlCenter", "com.boswas.SecurityCenter", "com.boswas.SoftwareCenter",
                             "com.boswas.UpdateCenter"})
BOSWAS, WINDOWS, NATIVE = "boswas", "windows", "native"
GROUP_TITLES = {BOSWAS: "Boswas applications", WINDOWS: "Windows applications", NATIVE: "Applications"}
GROUP_ORDER = (BOSWAS, WINDOWS, NATIVE)


def valid_desktop_id(value: object) -> bool:
    return isinstance(value, str) and bool(_DESKTOP_ID_RE.fullmatch(value))


def _unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(_ESCAPES.get(value[i + 1], value[i + 1]))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _list_value(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(";") if part.strip())


def locale_keys(locale: object) -> list[str]:
    """Localised key suffixes to try, most specific first: de_DE.UTF-8@euro -> de_DE@euro, de_DE, de@euro, de."""
    if not isinstance(locale, str) or not locale or locale in ("C", "POSIX"):
        return []
    text, _, modifier = locale.partition("@")
    text = text.split(".", 1)[0]
    lang, _, country = text.partition("_")
    keys = []
    if country and modifier:
        keys.append(f"{lang}_{country}@{modifier}")
    if country:
        keys.append(f"{lang}_{country}")
    if modifier:
        keys.append(f"{lang}@{modifier}")
    if lang:
        keys.append(lang)
    return keys


def parse_desktop_group(text: object, limit: int = 400) -> dict[str, str]:
    """The [Desktop Entry] group as {key: raw value}; the first occurrence of a key wins."""
    values: dict[str, str] = {}
    current = None
    for raw in text.splitlines() if isinstance(text, str) else []:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _GROUP_RE.match(line)
        if match:
            if current == "Desktop Entry":
                break                     # the main group is complete
            current = match.group(1)
            continue
        if current != "Desktop Entry" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in values:
            values[key] = value.strip()[:4096]
        if len(values) > limit:
            break
    return values


@dataclass(frozen=True)
class DesktopEntry:
    desktop_id: str              # without ".desktop"
    path: str
    name: str
    generic_name: str = ""
    comment: str = ""
    icon: str = ""
    categories: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    winapp_id: str = ""


def parse_desktop_entry(text: object, desktop_id: str, path: str, locale: object = None, desktop: str = "KDE",
                        which=None) -> DesktopEntry | None:
    """A launchable application entry, or None when the entry must not be shown in KDE.

    Follows the Desktop Entry Specification: Type=Application only; skipped
    when NoDisplay or Hidden is true, when OnlyShowIn does not include the
    desktop or NotShowIn does, when Name or Exec is missing, or when TryExec
    names a program that is not installed (which(program) is None).
    """
    if not valid_desktop_id(desktop_id):
        return None
    group = parse_desktop_group(text)
    if group.get("Type") != "Application":
        return None
    if group.get("NoDisplay", "").lower() == "true" or group.get("Hidden", "").lower() == "true":
        return None
    only = _list_value(group.get("OnlyShowIn"))
    if only and desktop not in only:
        return None
    if desktop in _list_value(group.get("NotShowIn")):
        return None
    if not group.get("Exec") and group.get("DBusActivatable", "").lower() != "true":
        return None
    try_exec = _unescape(group.get("TryExec", "")).strip()
    if try_exec and which is not None and which(try_exec) is None:
        return None

    def localised(key: str, limit: int) -> str:
        for suffix in locale_keys(locale):
            value = group.get(f"{key}[{suffix}]")
            if value:
                return one_line(_unescape(value), limit)
        return one_line(_unescape(group.get(key, "")), limit)

    name = localised("Name", 120)
    if not name:
        return None
    keywords = tuple(one_line(k, 40) for k in _list_value(localised("Keywords", 400)))
    return DesktopEntry(
        desktop_id=desktop_id, path=path, name=name, generic_name=localised("GenericName", 120),
        comment=localised("Comment", 300), icon=one_line(_unescape(group.get("Icon", "")), 300),
        categories=_list_value(group.get("Categories")), keywords=keywords,
        winapp_id=one_line(group.get("X-Boswas-WinApp-Id"), 96))


@dataclass(frozen=True)
class AppItem:
    key: str
    group: str
    name: str
    subtitle: str
    description: str
    icon: str
    desktop_id: str = ""
    path: str = ""
    windows_id: str = ""
    search: str = field(default="", repr=False)

    @property
    def launchable(self) -> bool:
        return self.group != WINDOWS and bool(self.desktop_id)


def _entry_item(entry: DesktopEntry, group: str) -> AppItem:
    subtitle = entry.generic_name if entry.generic_name and entry.generic_name != entry.name else ""
    search = " ".join((entry.name, entry.generic_name, entry.comment, entry.desktop_id, *entry.keywords)).casefold()
    return AppItem(f"desktop:{entry.desktop_id}", group, entry.name, subtitle or entry.comment, entry.comment,
                   entry.icon, entry.desktop_id, entry.path, "", search)


def windows_item(app: dict) -> AppItem | None:
    app_id = one_line(app.get("id"), 96)
    if not app_id:
        return None
    name = one_line(app.get("name"), 120) or app_id
    state = WINDOWS_APP_STATES.get(app.get("app_state"), "")
    version = one_line(app.get("version"), 40)
    publisher = one_line(app.get("publisher"), 80)
    parts = ["Windows application"] + ([f"version {version}"] if version else []) + ([state] if state else [])
    description = (f"Published by {publisher}. " if publisher else "") + "Runs in its own Boswas sandbox."
    search = " ".join((name, app_id, publisher, "windows")).casefold()
    return AppItem(f"windows:{app_id}", WINDOWS, name, " · ".join(parts), description,
                   "application-x-ms-dos-executable", "", "", app_id, search)


def application_groups(entries: object, windows_apps: object) -> dict[str, list[AppItem]]:
    """Installed applications in three groups, each sorted by name.

    boswas   desktop entries com.boswas.* (Control Center's own entries excluded)
    windows  the session agent's apps.list (their launchers are left out of the
             other groups: Windows applications are started from the
             Compatibility Manager or the application menu, never from here)
    native   every other visible desktop entry
    """
    groups: dict[str, list[AppItem]] = {BOSWAS: [], WINDOWS: [], NATIVE: []}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, DesktopEntry) or entry.desktop_id in OWN_DESKTOP_IDS:
            continue
        if entry.winapp_id or entry.desktop_id.startswith("boswas-winapp-"):
            continue
        group = BOSWAS if entry.desktop_id.startswith("com.boswas.") else NATIVE
        groups[group].append(_entry_item(entry, group))
    for app in windows_apps if isinstance(windows_apps, list) else []:
        item = windows_item(app) if isinstance(app, dict) else None
        if item is not None:
            groups[WINDOWS].append(item)
    for items in groups.values():
        items.sort(key=lambda item: (item.name.casefold(), item.key))
    return groups


def filter_apps(items: list[AppItem], text: object) -> list[AppItem]:
    words = one_line(text, 100).casefold().split()
    return [item for item in items if all(word in item.search for word in words)]


# --- presets (boswas-preset --json list) ----------------------------------------------------------------

PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ACCENT_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
PREVIEW_SUFFIXES = (".png", ".jpg", ".jpeg", ".svg", ".webp")
VARIANT_LABELS = {"light": "Light", "dark": "Dark"}


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    description: str
    variant: str
    accent: str | None
    preview: str | None
    current: bool

    @property
    def variant_label(self) -> str:
        return VARIANT_LABELS.get(self.variant, "")


@dataclass(frozen=True)
class PresetList:
    presets: tuple[Preset, ...]
    current: str | None


def valid_preset_id(value: object) -> bool:
    return isinstance(value, str) and bool(PRESET_ID_RE.fullmatch(value))


def preview_path(value: object) -> str | None:
    """An absolute path to a preview image (no '..' component, image suffix), else None."""
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value or len(value) > 1024:
        return None
    if ".." in value.split("/") or not value.lower().endswith(PREVIEW_SUFFIXES):
        return None
    return value


def parse_presets(doc: object) -> PresetList:
    """The CONTRACT of `boswas-preset --json list`:

      {"presets": [{"id", "name", "description", "variant", "accent", "preview"}], "current": id | null}

    Entries with an invalid ID are left out; a document of another shape raises ValueError.
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("presets"), list):
        raise ValueError("boswas-preset returned an unexpected document")
    current = doc.get("current") if valid_preset_id(doc.get("current")) else None
    presets, seen = [], set()
    for item in doc["presets"]:
        if not isinstance(item, dict) or not valid_preset_id(item.get("id")) or item["id"] in seen:
            continue
        seen.add(item["id"])
        variant = item.get("variant") if item.get("variant") in VARIANT_LABELS else ""
        accent = item.get("accent") if isinstance(item.get("accent"), str) and _ACCENT_RE.match(item["accent"]) \
            else None
        presets.append(Preset(item["id"], one_line(item.get("name"), 80) or item["id"],
                              one_line(item.get("description"), 300), variant, accent,
                              preview_path(item.get("preview")), item["id"] == current))
    return PresetList(tuple(presets), current)
