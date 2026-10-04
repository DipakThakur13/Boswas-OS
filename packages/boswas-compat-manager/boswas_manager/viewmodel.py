"""Presentation rules of the Compatibility Manager (pure Python, no Qt).

The widgets only display what this module derives from the backend's
documents: labels, filters, which actions are possible, the install flow,
the removal plan and the permission, detail and status rows. That keeps the
rules testable without a display, and keeps decisions out of the GUI: it
never decides what an application may do, it explains what the session
agent reports.

Every string from the backend is untrusted (application names, messages and
log lines can be chosen by Windows software or installers) and goes through
sanitize() or one_line() before it reaches a widget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo

# --- fixed texts ------------------------------------------------------------------------------

# Boswas OS runs 64-bit Windows applications only (product decision, ADR-0014).
# These are fixed product texts: no workaround is ever offered next to them.
UNSUPPORTED_32BIT_MESSAGE = ("This application requires 32-bit Windows compatibility, "
                             "which is not supported by Boswas OS.")
UNSUPPORTED_32BIT_DETAIL = "32-bit Windows applications are not supported by this version of Boswas OS."
SUPPORTED_ARCHITECTURE = "x86_64 / 64-bit Windows applications"
WINDOWS_APPLICATIONS = "x86_64 / 64-bit only"

PERMISSIONS_NOTICE = ("Permissions come from the Boswas compatibility catalog and device policy. "
                      "They cannot be changed here.")
REMOVABLE_DEVICES = "Never available to Windows applications"
REMOVE_WARNING = ("Files the application saved inside its own C: drive are deleted. "
                  "Files in folders it was granted (for example Documents) are kept.")
STOP_WARNING = ("Stopping ends all of the application's Windows processes, like “End task”. "
                "Unsaved work in the application is lost.")
SESSION_UNAVAILABLE = ("The Boswas session service is not running, so Windows applications cannot be managed "
                       "right now. It starts automatically when you log in: log out and back in, or contact your "
                       "administrator if this message does not go away.")
CHECKING_INSTALLER = "Checking the installer…"
CHECKING_NOTE = "Nothing is installed or run while the installer is checked."
ADMINISTRATOR = "requested by your administrator"
# Shown instead of the real location: the full path would reveal /home/<user>.
PREFIX_DISPLAY_ROOT = "~/.local/share/boswas/wine"

# --- untrusted text -------------------------------------------------------------------------

# Bidirectional embedding, override and isolate controls (U+202A-U+202E, U+2066-U+2069), built from code
# points so the source itself contains no invisible characters.
_BIDI_CONTROLS = "".join(map(chr, (*range(0x202A, 0x202F), *range(0x2066, 0x206A))))
_CONTROL_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"                         # CSI (colours, cursor movement)
    r"|\x1b\][^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"    # OSC (titles, hyperlinks)
    r"|\x1b[PX^_][^\x1b\x9c]*(?:\x1b\\|\x9c)?"         # DCS, SOS, PM and APC strings
    r"|\x1b[ -/]*[0-~]?"                               # any other escape sequence
    r"|\x9b[0-?]*[ -/]*[@-~]"                          # 8-bit CSI
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"                  # C0 except tab and newline, DEL, C1
    rf"|[{_BIDI_CONTROLS}]"                            # bidirectional overrides (spoofed names)
)
_HOME_RE = re.compile(r"/home/[^/\s]+")
_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")


def sanitize(value: object) -> str:
    """Text without escape sequences or control characters (newline and tab stay)."""
    if value is None:
        return ""
    return _CONTROL_RE.sub("", value if isinstance(value, str) else str(value))


def one_line(value: object, limit: int = 200) -> str:
    """A sanitised single line, whitespace collapsed, shortened with an ellipsis."""
    text = " ".join(sanitize(value).split())
    return text if len(text) <= limit else text[:max(1, limit - 1)].rstrip() + "…"


def abbreviate_home(text: str) -> str:
    """Replace /home/<user> by ~ (messages can quote paths; screenshots get shared)."""
    return _HOME_RE.sub("~", text)


def message_text(value: object, limit: int = 2000) -> str:
    """A backend message for display: sanitised, home abbreviated, bounded."""
    text = abbreviate_home(sanitize(value)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def valid_id(value: object) -> bool:
    """Application ID syntax (lower-case reverse DNS); the backend is the authority, this is for hints."""
    return isinstance(value, str) and len(value) <= 96 and bool(_ID_RE.fullmatch(value))


def file_name(path: object) -> str:
    return one_line(str(path or "").rstrip("/").rsplit("/", 1)[-1], 120)


def format_time(value: object, tz: tzinfo | None = None) -> str:
    """An API timestamp (ISO 8601, UTC) as local "YYYY-MM-DD HH:MM"; "" when absent."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return one_line(value, 40)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def format_size(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return "Unknown"
    size = float(value)
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return "Unknown"


def _human_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# --- application states -----------------------------------------------------------------------

APP_STATES = ("INSTALLING", "INSTALLED", "RUNNING", "STOPPED", "ERROR", "REPAIR_REQUIRED", "BLOCKED", "UNSUPPORTED")
STATE_LABELS = {
    "INSTALLING": "Installing", "INSTALLED": "Installed", "RUNNING": "Running", "STOPPED": "Stopped",
    "ERROR": "Error", "REPAIR_REQUIRED": "Repair required", "BLOCKED": "Blocked", "UNSUPPORTED": "Unsupported",
}
# Status-dot level of each state: ok, active, warning, error or neutral.
STATE_LEVELS = {
    "INSTALLING": "neutral", "INSTALLED": "ok", "RUNNING": "active", "STOPPED": "ok", "ERROR": "error",
    "REPAIR_REQUIRED": "warning", "BLOCKED": "error", "UNSUPPORTED": "error",
}
ATTENTION_STATES = frozenset({"ERROR", "REPAIR_REQUIRED", "BLOCKED", "UNSUPPORTED"})
BUSY_LABELS = {"install": "Installing…", "repair": "Repairing…", "remove": "Removing…",
               "upgrade": "Updating…"}
CATALOG_LABELS = {"approved": "Approved", "tested": "Tested", "experimental": "Experimental",
                  "untested": "Untested", "unknown": "Unknown", "blocked": "Blocked"}
CATALOG_LEVELS = {"approved": "ok", "tested": "ok", "experimental": "warning", "untested": "warning",
                  "unknown": "neutral", "blocked": "error"}
LAYER_LABELS = {"managed": "Boswas compatibility catalog (from your organisation)",
                "local": "Boswas compatibility catalog (this device's administrator)",
                "system": "Boswas compatibility catalog (Boswas OS)"}
FOLDER_LABELS = {"documents": "Documents", "downloads": "Downloads", "desktop": "Desktop", "pictures": "Pictures",
                 "music": "Music", "videos": "Videos"}


def architecture_label(value: object) -> str:
    if value == "x86_64":
        return "x86_64"
    if value == "x86":
        return "x86 (32-bit)"
    return one_line(value, 40) if value else "Unknown"


def source_label(value: object) -> str:
    if value in (None, "", "unlisted"):
        return "Not in the Boswas compatibility catalog (device policy rules apply)"
    return LAYER_LABELS.get(value, f"Boswas compatibility catalog ({one_line(value, 40)})")


def last_launch_text(last: object, tz: tzinfo | None = None) -> str:
    if not isinstance(last, dict):
        return "Never"
    when = format_time(last.get("at"), tz) or "Unknown time"
    code = last.get("exit_code")
    if last.get("stopped") is True:
        outcome = "stopped"
    elif last.get("timed_out") is True:
        outcome = "timed out"
    elif code == 0 and not isinstance(code, bool):
        outcome = "exited normally"
    elif isinstance(code, int) and not isinstance(code, bool):
        outcome = f"exit code {code}"
    else:
        outcome = ""
    return f"{when} ({outcome})" if outcome else when


@dataclass(frozen=True)
class AppRow:
    """One installed application, ready for display (all strings sanitised)."""

    id: str
    name: str
    version: str
    publisher: str
    architecture: str
    state: str
    state_label: str
    state_reason: str
    compatibility: str
    compatibility_label: str
    last_launch: str
    last_launch_at: str
    busy: str | None = None
    update_version: str | None = None
    damaged: bool = False
    source: str = "unlisted"

    @property
    def running(self) -> bool:
        return self.state == "RUNNING"

    @property
    def running_label(self) -> str:
        return "Running" if self.running else "Not running"

    @property
    def display_state(self) -> str:
        return BUSY_LABELS.get(self.busy or "", "Busy…") if self.busy else self.state_label

    @property
    def level(self) -> str:
        return "neutral" if self.busy else STATE_LEVELS[self.state]


def catalog_index(manifests: object) -> dict[str, dict]:
    if not isinstance(manifests, list):
        return {}
    return {m["id"]: m for m in manifests if isinstance(m, dict) and isinstance(m.get("id"), str)}


def available_update(app: dict, manifest: dict | None) -> str | None:
    """The catalog version when it differs from the installed one (same application ID)."""
    if not isinstance(manifest, dict) or not isinstance(app, dict) or app.get("state") == "damaged":
        return None
    if app.get("manifest_source") in (None, "unlisted") or manifest.get("status") == "blocked":
        return None                  # an unlisted install with a catalog ID is a different application
    latest, installed = manifest.get("version"), app.get("version")
    if not isinstance(latest, str) or not latest or latest == "unknown" or not isinstance(installed, str):
        return None
    return one_line(latest, 40) if latest != installed else None


def app_row(app: dict, manifest: dict | None = None, tz: tzinfo | None = None) -> AppRow:
    app_id = one_line(app.get("id"), 96) or "?"
    known = app.get("app_state") in APP_STATES
    state = app.get("app_state") if known else "ERROR"
    reason = one_line(app.get("state_reason"), 500)
    if not known and not reason:
        reason = "The session service reported an unknown state."
    status = app.get("status") if app.get("status") in CATALOG_LABELS else "unknown"
    last = app.get("last_launch")
    return AppRow(
        id=app_id,
        name=one_line(app.get("name"), 120) or app_id,
        version=one_line(app.get("version"), 40) or "Unknown",
        publisher=one_line(app.get("publisher"), 80) or "Unknown",
        architecture=architecture_label(app.get("architecture")),
        state=state,
        state_label=STATE_LABELS[state],
        state_reason=reason,
        compatibility=status,
        compatibility_label=CATALOG_LABELS[status],
        last_launch=last_launch_text(last, tz),
        last_launch_at=last.get("at") if isinstance(last, dict) and isinstance(last.get("at"), str) else "",
        busy=one_line(app.get("busy"), 20) or None,
        update_version=available_update(app, manifest),
        damaged=app.get("state") == "damaged",
        source=one_line(app.get("manifest_source"), 40) or "unlisted",
    )


def app_rows(applications: object, manifests: object = None, tz: tzinfo | None = None) -> list[AppRow]:
    index = catalog_index(manifests)
    rows = [app_row(app, index.get(app.get("id")), tz) for app in applications or [] if isinstance(app, dict)]
    return sorted(rows, key=lambda r: (r.name.casefold(), r.id))


# --- library filters and actions -------------------------------------------------------------

FILTERS = (
    ("all", "All Applications"),
    ("installed", "Installed"),
    ("running", "Running"),
    ("updates", "Updates"),
    ("blocked", "Blocked"),
    ("repair", "Repair Required"),
    ("unsupported", "Unsupported"),
)
FILTER_LABELS = dict(FILTERS)
_FILTER_STATES = {
    "installed": {"INSTALLED", "STOPPED", "RUNNING"},     # ready to use
    "running": {"RUNNING"},
    "blocked": {"BLOCKED"},
    "repair": {"REPAIR_REQUIRED", "ERROR"},               # both are fixed by repairing or reinstalling
    "unsupported": {"UNSUPPORTED"},
}


def matches_filter(row: AppRow, key: str) -> bool:
    if key == "all":
        return True
    if key == "updates":
        return row.update_version is not None
    states = _FILTER_STATES.get(key)
    if states is None:
        raise ValueError(f"unknown filter {key!r}")
    return row.state in states


def matches_search(row: AppRow, text: str) -> bool:
    needle = " ".join(sanitize(text).split()).casefold()
    if not needle:
        return True
    return any(needle in field.casefold() for field in (row.name, row.id, row.publisher, row.version))


def filter_rows(rows: list[AppRow], key: str = "all", text: str = "") -> list[AppRow]:
    return [row for row in rows if matches_filter(row, key) and matches_search(row, text)]


def filter_counts(rows: list[AppRow]) -> dict[str, int]:
    return {key: sum(1 for row in rows if matches_filter(row, key)) for key, _label in FILTERS}


ACTIONS = ("launch", "stop", "repair", "update", "remove", "logs", "details")


def available_actions(row: AppRow | None) -> dict[str, bool]:
    """Which buttons are enabled. The backend re-checks everything; this only avoids offering the impossible."""
    if row is None:
        return dict.fromkeys(ACTIONS, False)
    idle = not row.busy
    state = row.state
    return {
        "launch": idle and state in ("INSTALLED", "STOPPED"),
        "stop": idle and state == "RUNNING",
        # A damaged entry has no installation record to repair from: only removal helps.
        "repair": idle and not row.damaged and state in ("INSTALLED", "STOPPED", "ERROR", "REPAIR_REQUIRED"),
        "update": idle and not row.damaged and state in ("INSTALLED", "STOPPED"),
        "remove": idle and state not in ("RUNNING", "INSTALLING"),
        "logs": True,
        "details": True,
    }


# --- dashboard ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class DashboardSummary:
    installed: int
    running: int
    attention: int
    updates: int


def dashboard_summary(rows: list[AppRow]) -> DashboardSummary:
    return DashboardSummary(
        installed=sum(1 for r in rows if r.state != "INSTALLING"),
        running=sum(1 for r in rows if r.state == "RUNNING"),
        attention=sum(1 for r in rows if r.state in ATTENTION_STATES),
        updates=sum(1 for r in rows if r.update_version is not None),
    )


@dataclass(frozen=True)
class ActivityItem:
    when: str
    sort_key: str
    text: str
    remote: bool


EVENT_LABELS = {
    "install.succeeded": "Installed", "install.failed": "Installation failed",
    "install.refused": "Installation refused", "upgrade.succeeded": "Updated", "upgrade.failed": "Update failed",
    "repair.succeeded": "Repaired", "repair.failed": "Repair failed", "remove.succeeded": "Removed",
    "remove.failed": "Removal failed", "launch.started": "Started", "launch.finished": "Finished",
    "launch.failed": "Could not start", "policy-refused": "Refused by policy", "stop": "Stopped",
}
COMMAND_LABELS = {
    "INSTALL_APPLICATION": "Install", "UPDATE_APPLICATION": "Update", "REMOVE_APPLICATION": "Remove",
    "LAUNCH_APPLICATION": "Start", "STOP_APPLICATION": "Stop", "REPAIR_APPLICATION": "Repair",
    "REFRESH_INVENTORY": "Refresh inventory", "APPLY_POLICY": "Apply policy", "UPDATE_AGENT": "Update device agent",
}
COMMAND_STATUS_LABELS = {
    "QUEUED": "queued", "SENT": "sent", "ACKNOWLEDGED": "received", "RUNNING": "in progress",
    "SUCCEEDED": "completed", "FAILED": "failed", "EXPIRED": "expired", "CANCELLED": "cancelled",
}


def activity_items(events: object, commands: object, limit: int = 30, tz: tzinfo | None = None) -> list[ActivityItem]:
    """Recent local activity and remote management actions, newest first."""
    items: list[ActivityItem] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        label = EVENT_LABELS.get(kind) or one_line(kind.replace(".", " ").replace("-", " ").capitalize(), 40)
        detail = one_line(event.get("detail"), 300)
        text = f"{label}: {detail}" if detail else label
        remote = event.get("source") == "remote"
        if remote:
            text += f" — {ADMINISTRATOR}"
        at = event.get("occurred_at") if isinstance(event.get("occurred_at"), str) else ""
        items.append(ActivityItem(format_time(at, tz), at, message_text(text, 400), remote))
    for command in commands if isinstance(commands, list) else []:
        if not isinstance(command, dict):
            continue
        ctype = str(command.get("type") or "")
        label = COMMAND_LABELS.get(ctype) or one_line(ctype.replace("_", " ").capitalize(), 40) or "Command"
        target = one_line(command.get("application_id"), 96)
        status = COMMAND_STATUS_LABELS.get(str(command.get("status")), one_line(command.get("status"), 20).lower())
        text = f"{label} {target}".strip() + (f": {status}" if status else "")
        if command.get("error_code"):
            text += f" ({one_line(command.get('error_code'), 40)})"
        text += f" — {ADMINISTRATOR}"
        at = command.get("finished_at") or command.get("received_at")
        at = at if isinstance(at, str) else ""
        items.append(ActivityItem(format_time(at, tz), at, text, True))
    items.sort(key=lambda item: item.sort_key, reverse=True)
    return items[:limit]


# --- system status -------------------------------------------------------------------------------

@dataclass(frozen=True)
class StatusRow:
    key: str
    label: str
    value: str
    level: str          # ok, active, warning, error or neutral


APPARMOR_LABELS = {"enforce": "Enforced", "complain": "Complain mode", "not-loaded": "Not loaded",
                   "unavailable": "Unavailable", "unknown": "Unknown"}
APPARMOR_LEVELS = {"enforce": "ok", "complain": "warning", "not-loaded": "error", "unavailable": "error",
                   "unknown": "neutral"}
DEVICE_STATE_LABELS = {"INITIALIZING": "Starting", "READY": "Ready", "DEGRADED": "Degraded", "OFFLINE": "Offline",
                       "UPDATING": "Updating", "ERROR": "Error", "MAINTENANCE": "Maintenance"}
DEVICE_STATE_LEVELS = {"INITIALIZING": "neutral", "READY": "ok", "DEGRADED": "warning", "OFFLINE": "warning",
                       "UPDATING": "neutral", "ERROR": "error", "MAINTENANCE": "warning"}
CONNECTION_LABELS = {"CONNECTED": "Connected", "OFFLINE": "Offline", "STANDALONE": "Standalone",
                     "UNENROLLED": "Not enrolled", "REVOKED": "Revoked"}
CONNECTION_LEVELS = {"CONNECTED": "ok", "OFFLINE": "warning", "STANDALONE": "neutral", "UNENROLLED": "neutral",
                     "REVOKED": "error"}
SYSTEM_ROW_LABELS = (
    ("wine", "Wine"), ("apparmor", "AppArmor"), ("bubblewrap", "Bubblewrap"), ("agent", "Device Agent"),
    ("control_plane", "Control Plane"), ("disk", "Disk space"), ("architecture", "System architecture"),
    ("windows", "Windows applications"), ("os_version", "Boswas OS version"), ("policy", "Policy"),
)


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def agent_running(agent: object) -> bool:
    """A device agent status document (not the session agent's {"available": false} placeholder)."""
    return isinstance(agent, dict) and agent.get("available") is not False and "state" in agent


def apparmor_mode(system: object) -> str | None:
    mode = _dict(_dict(_dict(system).get("runtime")).get("apparmor")).get("mode")
    return mode if isinstance(mode, str) else None


def wine_row(system: object) -> StatusRow:
    if not isinstance(system, dict):
        return StatusRow("wine", "Wine", "Unknown", "neutral")
    wine = _dict(_dict(system.get("runtime")).get("wine"))
    if wine.get("available") is True:
        version = one_line(wine.get("version"), 40)
        return StatusRow("wine", "Wine", f"Healthy ({version})" if version else "Healthy", "ok")
    return StatusRow("wine", "Wine", "Missing", "error")


def apparmor_row(system: object) -> StatusRow:
    if not isinstance(system, dict):
        return StatusRow("apparmor", "AppArmor", "Unknown", "neutral")
    mode = apparmor_mode(system)
    mode = mode if mode in APPARMOR_LABELS else "unknown"
    return StatusRow("apparmor", "AppArmor", APPARMOR_LABELS[mode], APPARMOR_LEVELS[mode])


def bubblewrap_row(system: object) -> StatusRow:
    if not isinstance(system, dict):
        return StatusRow("bubblewrap", "Bubblewrap", "Unknown", "neutral")
    bubblewrap = _dict(_dict(system.get("runtime")).get("bubblewrap"))
    if bubblewrap.get("available") is True:
        version = one_line(bubblewrap.get("version"), 20)
        return StatusRow("bubblewrap", "Bubblewrap", f"Available ({version})" if version else "Available", "ok")
    return StatusRow("bubblewrap", "Bubblewrap", "Missing", "error")


def agent_row(agent: object) -> StatusRow:
    if not agent_running(agent):
        return StatusRow("agent", "Device Agent", "Not running", "warning")
    state = agent.get("state")
    label = DEVICE_STATE_LABELS.get(state, one_line(state, 20).capitalize() or "Unknown")
    return StatusRow("agent", "Device Agent", f"Running ({label})", DEVICE_STATE_LEVELS.get(state, "neutral"))


def control_plane_row(agent: object) -> StatusRow:
    if not agent_running(agent):
        return StatusRow("control_plane", "Control Plane", "Unknown", "neutral")
    connection = agent.get("connection")
    if connection not in CONNECTION_LABELS:
        return StatusRow("control_plane", "Control Plane", "Unknown", "neutral")
    return StatusRow("control_plane", "Control Plane", CONNECTION_LABELS[connection], CONNECTION_LEVELS[connection])


def policy_row(system: object, agent: object) -> StatusRow:
    if agent_running(agent):
        policy = _dict(agent.get("policy"))
        if policy.get("managed") is True:
            version = one_line(policy.get("version"), 40)
            return StatusRow("policy", "Policy", f"Managed: {version}" if version else "Managed", "ok")
        return StatusRow("policy", "Policy", "Local", "neutral")
    policy = _dict(_dict(_dict(system).get("runtime")).get("policy"))
    if not isinstance(system, dict) or "managed" not in policy:
        return StatusRow("policy", "Policy", "Unknown", "neutral")
    return StatusRow("policy", "Policy", "Managed" if policy.get("managed") is True else "Local",
                     "ok" if policy.get("managed") is True else "neutral")


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def system_rows(system: object, agent: object) -> list[StatusRow]:
    """The System Status page, in its fixed order.

    system is the session agent's system.status (None when the session
    service is unavailable), agent the device agent's agent.status (None
    when the device agent is not running).
    """
    system_doc = _dict(system)
    os_doc = _dict(system_doc.get("os"))
    disk = _dict(system_doc.get("disk"))
    free, total = _number(disk.get("free_gib")), _number(disk.get("total_gib"))
    if free is not None and total is not None:
        disk_row = StatusRow("disk", "Disk space", f"{free:.1f} GiB free of {total:.1f} GiB",
                             "warning" if free < 2 else "ok")
    else:
        disk_row = StatusRow("disk", "Disk space", "Unknown", "neutral")
    version = one_line(os_doc.get("version") or os_doc.get("version_id"), 40)
    return [
        wine_row(system),
        apparmor_row(system),
        bubblewrap_row(system),
        agent_row(agent),
        control_plane_row(agent),
        disk_row,
        StatusRow("architecture", "System architecture", one_line(os_doc.get("architecture"), 20) or "Unknown",
                  "neutral"),
        StatusRow("windows", "Windows applications", WINDOWS_APPLICATIONS, "neutral"),
        StatusRow("os_version", "Boswas OS version", version or "Unknown", "neutral"),
        policy_row(system, agent),
    ]


@dataclass(frozen=True)
class HeaderStatus:
    device: str
    device_level: str
    control_plane: str
    control_plane_level: str


def header_status(agent: object, checked: bool = True) -> HeaderStatus:
    """The at-a-glance line at the top of the window."""
    if not checked:
        return HeaderStatus("Device: checking…", "neutral", "Control Plane: checking…", "neutral")
    if not agent_running(agent):
        return HeaderStatus("Device agent: not running", "warning", "Control Plane: unknown", "neutral")
    state = agent.get("state")
    device = DEVICE_STATE_LABELS.get(state, "Unknown")
    plane = control_plane_row(agent)
    return HeaderStatus(f"Device: {device}", DEVICE_STATE_LEVELS.get(state, "neutral"),
                        f"Control Plane: {plane.value}", plane.level)


# --- details and permissions -------------------------------------------------------------------------

@dataclass(frozen=True)
class DetailRow:
    label: str
    value: str


@dataclass(frozen=True)
class PermissionRow:
    """One read-only line of the Permissions tab (granted: None when not a yes/no question)."""

    key: str
    label: str
    value: str
    granted: bool | None


def prefix_display(app_id: object) -> str:
    """The application's data and C: drive location as shown to the user (never the real /home path)."""
    return f"{PREFIX_DISPLAY_ROOT}/{app_id}" if valid_id(app_id) else f"{PREFIX_DISPLAY_ROOT}/…"


def files_text(folders: object) -> str:
    names = [FOLDER_LABELS[f] for f in folders if f in FOLDER_LABELS] if isinstance(folders, list) else []
    if not names:
        return "Only its own C: drive"
    noun = "folder" if len(names) == 1 else "folders"
    return f"Its own C: drive, and your {_human_list(names)} {noun}"


def sandbox_rows(sandbox: object) -> list[PermissionRow]:
    """What an application's sandbox contains, from the catalog and policy (as reported by the backend)."""
    grants = _dict(sandbox)

    def flag(key: str, label: str, yes: str = "Allowed", no: str = "Not allowed") -> PermissionRow:
        granted = grants.get(key) is True
        return PermissionRow(key, label, yes if granted else no, granted)

    folders = grants.get("folders")
    return [
        PermissionRow("files", "Files", files_text(folders), bool(folders)),
        flag("network", "Network", no="Blocked"),
        flag("display", "Display"),
        flag("audio", "Audio"),
        flag("gpu", "GPU"),
        PermissionRow("removable", "Removable devices", REMOVABLE_DEVICES, False),
    ]


CONFINEMENT_LABELS = {"enforce": "Enforced", "complain": "Complain mode (not enforced)",
                      "not-loaded": "Profile not loaded", "unavailable": "AppArmor unavailable"}


def confinement_text(mode: object, profile: object = None, last_confinement: object = None) -> str:
    """Windows code always runs under the Boswas AppArmor profile; this says how it is enforced right now."""
    name = one_line(profile, 60)
    profile_text = f"the {name} profile" if name else "the Boswas AppArmor profile"
    if mode in CONFINEMENT_LABELS:
        text = f"{CONFINEMENT_LABELS[mode]} ({profile_text})"
    else:
        text = f"Always applied ({profile_text})"
    last = one_line(last_confinement, 80)
    return f"{text}; last run: {last}" if last else text


def permission_rows(sandbox: object, apparmor: object = None, profile: object = None,
                    last_confinement: object = None) -> list[PermissionRow]:
    rows = sandbox_rows(sandbox)
    rows.append(PermissionRow("apparmor", "AppArmor confinement",
                              confinement_text(apparmor, profile, last_confinement), None))
    return rows


def apparmor_profile(system: object) -> str | None:
    profile = _dict(_dict(_dict(system).get("runtime")).get("apparmor")).get("profile")
    return profile if isinstance(profile, str) else None


def policy_text(policy: object) -> str:
    doc = _dict(policy)
    if doc.get("allowed") is True:
        return "Allowed"
    reason = message_text(doc.get("reason"), 300)
    return f"Not allowed: {reason}" if reason else "Not allowed"


def executable_text(app: dict) -> str:
    launch = one_line(app.get("launch"), 200)
    if launch:
        return launch
    candidates = [one_line(c, 120) for c in app.get("launch_candidates") or [] if isinstance(c, str)]
    if candidates:
        return "Not chosen (candidates: " + ", ".join(candidates[:5]) + ")"
    return "Not known"


def detail_rows(status: object, tz: tzinfo | None = None) -> list[DetailRow]:
    """The Overview tab from apps.status. Real paths from the backend are never shown."""
    doc = _dict(status)
    app = _dict(doc.get("application"))
    app_id = one_line(app.get("id"), 96)
    state = app.get("app_state") if app.get("app_state") in APP_STATES else "ERROR"
    reason = message_text(app.get("state_reason"), 500)
    catalog = app.get("status") if app.get("status") in CATALOG_LABELS else "unknown"
    installer = _dict(doc.get("installer"))
    runtime = _dict(doc.get("runtime"))
    kind = {"exe": "Windows program (.exe)", "msi": "Windows Installer package (.msi)",
            "portable": "Portable Windows program"}.get(installer.get("kind"), "")
    rows = [
        DetailRow("Name", one_line(app.get("name"), 120) or app_id or "Unknown"),
        DetailRow("Application ID", app_id or "Unknown"),
        DetailRow("Version", one_line(app.get("version"), 40) or "Unknown"),
        DetailRow("Publisher", one_line(app.get("publisher"), 80) or "Unknown"),
        DetailRow("Executable", executable_text(app)),
        DetailRow("Architecture", architecture_label(app.get("architecture"))),
        DetailRow("Wine prefix", prefix_display(app.get("id"))),
        DetailRow("Installed", format_time(app.get("installed_at"), tz) or "Unknown"),
        DetailRow("Last run", last_launch_text(doc.get("last_launch"), tz)),
        DetailRow("State", f"{STATE_LABELS[state]} — {reason}" if reason else STATE_LABELS[state]),
        DetailRow("Catalog status", CATALOG_LABELS[catalog]),
        DetailRow("Policy decision", policy_text(doc.get("policy"))),
        DetailRow("Manifest source", source_label(app.get("manifest_source"))),
    ]
    if installer.get("file"):
        parts = [one_line(installer.get("file"), 120), kind]
        rows.append(DetailRow("Installer", " — ".join(part for part in parts if part)))
    if runtime.get("wine"):
        rows.append(DetailRow("Wine version", one_line(runtime.get("wine"), 60)))
    return rows


def is_running(status: object) -> bool:
    doc = _dict(status)
    return _dict(doc.get("application")).get("app_state") == "RUNNING"


# --- removal, stop, launch -------------------------------------------------------------------------

@dataclass(frozen=True)
class RemovalPlan:
    title: str
    intro: str
    items: tuple[str, ...]
    warning: str
    confirm_label: str = "Remove"


def removal_plan(name: str, app_id: str) -> RemovalPlan:
    name = one_line(name, 120) or one_line(app_id, 96)
    return RemovalPlan(
        title=f"Remove {name}?",
        intro="The following will be deleted:",
        items=(f"The application {name} ({one_line(app_id, 96)})",
               "Its Wine prefix (C: drive) and everything stored inside it",
               "Its launcher in the application menu"),
        warning=REMOVE_WARNING,
    )


def stop_question(name: str) -> tuple[str, str]:
    return f"Stop {one_line(name, 120)}?", STOP_WARNING


def launch_message(result: object, name: str) -> str:
    doc = _dict(result)
    name = one_line(name, 120)
    if doc.get("already_running"):
        return f"{name} is already running."
    if doc.get("finished"):
        code = doc.get("exit_code")
        return f"{name} started and has already exited" + (f" (exit code {code})." if isinstance(code, int) else ".")
    return f"{name} is starting."


_DISPLAY_RE = re.compile(r"^(?:unix)?:\d{1,4}(?:\.\d+)?$")
_WAYLAND_RE = re.compile(r"^wayland-[0-9]{1,3}$")


def display_params(environ) -> dict[str, str]:
    """The session's display for apps.launch: only variables that are set and well-formed.

    The session agent rejects malformed values; leaving one out lets it fall
    back to the user manager's environment instead of failing the launch.
    """
    params = {}
    display = environ.get("DISPLAY")
    if isinstance(display, str) and _DISPLAY_RE.fullmatch(display):
        params["display"] = display
    xauthority = environ.get("XAUTHORITY")
    if isinstance(xauthority, str) and xauthority.startswith("/") and "\0" not in xauthority \
            and len(xauthority) <= 4096:
        params["xauthority"] = xauthority
    wayland = environ.get("WAYLAND_DISPLAY")
    if isinstance(wayland, str) and _WAYLAND_RE.fullmatch(wayland):
        params["wayland_display"] = wayland
    return params


# --- jobs and errors ------------------------------------------------------------------------------

JOB_VERBS = {"install": ("Installing", "installed", "Installation"), "upgrade": ("Updating", "updated", "Update"),
             "repair": ("Repairing", "repaired", "Repair"), "remove": ("Removing", "removed", "Removal")}


def job_started_text(op: str, name: str) -> str:
    return f"{JOB_VERBS.get(op, ('Working on',))[0]} {one_line(name, 120)}…"


def job_finished_text(job: object, name: str) -> tuple[str, str]:
    """(level, text) for a finished job: "ok" or "error"."""
    doc = _dict(job)
    _verb, done, noun = JOB_VERBS.get(doc.get("op"), ("", "finished", "The operation"))
    name = one_line(name, 120)
    if doc.get("state") == "succeeded":
        result = _dict(doc.get("result"))
        if doc.get("op") == "repair" and result.get("healthy") is False:
            detail = message_text(result.get("error"), 500)
            return "error", f"Repair of {name} finished, but the application must be reinstalled." + \
                (f" {detail}" if detail else "")
        return "ok", f"{name} was {done}."
    error = _dict(doc.get("error"))
    message = message_text(error.get("message"), 1000) or "no reason was given"
    return "error", f"{noun} of {name} failed: {message}"


def error_text(code: object, message: object) -> str:
    text = message_text(message, 1000)
    return text or f"The operation failed ({one_line(code, 40) or 'unknown error'})."


LOG_KINDS = (("latest", "Latest"), ("launch", "Last run"), ("install", "Installation"), ("repair", "Repair"))
LOG_MISSING = {"latest": "This application has no logs yet.",
               "launch": "This application has not been started yet, so there is no run log.",
               "install": "There is no installation log for this application.",
               "repair": "This application has not been repaired, so there is no repair log."}


def log_text(result: object) -> str:
    lines = _dict(result).get("lines")
    return "\n".join(sanitize(line) for line in lines if isinstance(line, str)) if isinstance(lines, list) else ""


def log_title(result: object) -> str:
    """The log's file name only (the backend's full path contains the home directory)."""
    name = file_name(_dict(result).get("log"))
    return name or "No log"


# --- install flow ---------------------------------------------------------------------------------

class FlowError(Exception):
    """An install-flow step that does not fit the current state (a GUI bug, never user input)."""


def _detected_architecture(inspection: dict) -> object:
    arch = _dict(inspection.get("architecture"))
    return arch["detected"] if "detected" in arch else _dict(inspection.get("installer")).get("machine")


def is_32bit(inspection: object) -> bool:
    """Whether an inspect result refuses the installer for being 32-bit Windows software."""
    doc = _dict(inspection)
    arch = _dict(doc.get("architecture"))
    decision = _dict(doc.get("decision"))
    if _detected_architecture(doc) == "x86" and arch.get("supported") is not True:
        return True
    return decision.get("reason") == "architecture" and UNSUPPORTED_32BIT_MESSAGE in str(decision.get("message"))


REFUSAL_TITLES = {
    "already-installed": "This application is already installed",
    "blocked": "This installer is blocked",
    "unlisted-denied": "Only applications from the compatibility catalog can be installed",
    "status-not-allowed": "This application is not allowed on this device",
    "policy-blocked": "This application is blocked on this device",
    "policy-not-allowed": "This application is not allowed on this device",
    "installer-mismatch": "This installer does not match the catalog",
    "architecture": "This installer is not for 64-bit Windows",
    "bad-installer": "This is not a Windows installer",
    "up-to-date": "This version is already installed",
}
REFUSAL_HINTS = {
    "already-installed": "Open Applications to repair, update or remove it.",
    "blocked": "The Boswas compatibility catalog blocks this installer because it is known not to work safely on "
               "Boswas OS.",
    "unlisted-denied": "This installer is not in the Boswas compatibility catalog, and the device policy allows "
                       "only catalog applications. Ask your administrator if you need it.",
    "status-not-allowed": "The device policy does not allow applications with this compatibility status. Ask your "
                          "administrator if you need it.",
    "policy-blocked": "Your administrator has blocked this application.",
    "policy-not-allowed": "Your administrator allows only selected applications on this device.",
    "installer-mismatch": "The catalog expects a different installer file for this application. Download it again "
                          "from the publisher.",
    "architecture": f"Boswas OS runs {SUPPORTED_ARCHITECTURE} only.",
    "up-to-date": "The application was installed from this installer; there is nothing to update.",
}


class InstallFlow:
    """The steps of installing (or updating) one installer, independent of widgets.

      select -> checking -> refused_32bit | refused | confirm | failed
      confirm -> installing | refused | refused_32bit
      installing -> done | failed
      any finished step -> select (choose another file) or checking (new file)
    """

    SELECT = "select"
    CHECKING = "checking"
    REFUSED_32BIT = "refused_32bit"
    REFUSED = "refused"
    CONFIRM = "confirm"
    INSTALLING = "installing"
    DONE = "done"
    FAILED = "failed"
    STATES = (SELECT, CHECKING, REFUSED_32BIT, REFUSED, CONFIRM, INSTALLING, DONE, FAILED)
    _RESTARTABLE = (SELECT, REFUSED_32BIT, REFUSED, CONFIRM, DONE, FAILED)

    def __init__(self, upgrade_id: str | None = None, upgrade_name: str | None = None):
        self.upgrade_id = upgrade_id
        self.upgrade_name = one_line(upgrade_name, 120) or None
        self.reset()

    def reset(self) -> None:
        self.state = self.SELECT
        self.path: str | None = None
        self.app_id: str | None = None
        self.name: str | None = None
        self.inspection: dict = {}
        self.job_id: str | None = None
        self.progress: list[str] = []
        self.result: dict = {}
        self.error_code: str | None = None
        self.error_message = ""
        self.failed_phase: str | None = None

    @property
    def upgrading(self) -> bool:
        return self.upgrade_id is not None

    def _require(self, *states: str) -> None:
        if self.state not in states:
            raise FlowError(f"not possible while the flow is {self.state}")

    # --- transitions ---
    def select(self, path: str, app_id: str | None = None, name: str | None = None) -> str:
        self._require(*self._RESTARTABLE)
        if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
            raise FlowError("the installer must be given as an absolute path")
        self.reset()
        self.path = path
        if self.upgrading:
            self.app_id = self.upgrade_id
        else:
            self.app_id = (app_id or "").strip() or None
            self.name = (name or "").strip() or None
        self.state = self.CHECKING
        return self.state

    def inspect_params(self) -> dict:
        self._require(self.CHECKING)
        return {"path": self.path, "app_id": self.app_id, "name": self.name}

    def inspected(self, result: object) -> str:
        self._require(self.CHECKING)
        self.inspection = _dict(result)
        decision = _dict(self.inspection.get("decision"))
        reason = decision.get("reason")
        if is_32bit(self.inspection):
            self.state, self.error_code = self.REFUSED_32BIT, "ARCHITECTURE"
        elif decision.get("allowed") is True and not self.upgrading:
            self.state = self.CONFIRM
        elif self.upgrading and reason == "already-installed" and self.application_id == self.upgrade_id:
            self.state = self.CONFIRM      # updating needs the application to be installed
        else:
            self.state = self.REFUSED
            self.error_code = str(reason or "refused")
            self.error_message = message_text(decision.get("message")) or "The installer was refused."
            if self.upgrading and decision.get("allowed") is True:
                self.error_code = "installer-mismatch"
                self.error_message = "This installer belongs to a different application."
        return self.state

    def check_failed(self, code: str, message: str) -> str:
        self._require(self.CHECKING)
        self.state, self.failed_phase = self.FAILED, "check"
        self.error_code, self.error_message = code, error_text(code, message)
        return self.state

    def install_params(self) -> dict:
        self._require(self.CONFIRM)
        if self.upgrading:
            return {"app_id": self.upgrade_id, "path": self.path}
        return {"path": self.path, "app_id": self.app_id, "name": self.name}

    def started(self, job_id: str) -> str:
        self._require(self.CONFIRM)
        self.state, self.job_id, self.progress = self.INSTALLING, job_id, []
        return self.state

    def start_refused(self, code: str, message: str) -> str:
        self._require(self.CONFIRM)
        text = str(message)
        if code == "ARCHITECTURE" and (_detected_architecture(self.inspection) == "x86"
                                       or UNSUPPORTED_32BIT_MESSAGE in text):
            self.state, self.error_code = self.REFUSED_32BIT, code
        else:
            self.state = self.REFUSED
            self.error_code = str(code).lower().replace("_", "-")
            self.error_message = error_text(code, message)
        return self.state

    def start_failed(self, code: str, message: str) -> str:
        """Installing could not be started for a reason other than a refusal (service unavailable)."""
        self._require(self.CONFIRM)
        self.state, self.failed_phase = self.FAILED, "install"
        self.error_code, self.error_message = code, error_text(code, message)
        return self.state

    def job_update(self, job: object) -> str:
        self._require(self.INSTALLING)
        doc = _dict(job)
        lines = doc.get("progress")
        if isinstance(lines, list):
            self.progress = [one_line(line, 500) for line in lines if isinstance(line, str)]
        if doc.get("state") == "succeeded":
            self.state, self.result = self.DONE, _dict(doc.get("result"))
        elif doc.get("state") == "failed":
            error = _dict(doc.get("error"))
            self.state, self.failed_phase = self.FAILED, "install"
            self.error_code = one_line(error.get("code"), 40) or "FAILED"
            self.error_message = error_text(self.error_code, error.get("message"))
        return self.state

    def job_lost(self, code: str, message: str) -> str:
        """The job can no longer be followed (for example the session service restarted)."""
        self._require(self.INSTALLING)
        self.state, self.failed_phase = self.FAILED, "install"
        self.error_code, self.error_message = code, error_text(code, message)
        return self.state

    # --- presentation ---
    @property
    def file_name(self) -> str:
        return file_name(self.path)

    @property
    def application_id(self) -> str | None:
        for doc in (_dict(self.result.get("application")), _dict(self.inspection.get("application"))):
            if valid_id(doc.get("id")):
                return doc["id"]
        return self.upgrade_id or (self.app_id if valid_id(self.app_id) else None)

    @property
    def application_name(self) -> str:
        app = _dict(self.inspection.get("application"))
        return one_line(app.get("name"), 120) or self.upgrade_name or self.file_name or "the application"

    @property
    def unlisted(self) -> bool:
        return _dict(self.inspection.get("application")).get("manifest_source") in (None, "unlisted")

    def decision_rows(self) -> list[DetailRow]:
        installer = _dict(self.inspection.get("installer"))
        app = _dict(self.inspection.get("application"))
        detected = _detected_architecture(self.inspection)
        kind = installer.get("kind")
        rows = [
            DetailRow("File", one_line(installer.get("file"), 120) or self.file_name),
            DetailRow("Type", {"exe": "Windows program (.exe)", "msi": "Windows Installer package (.msi)"}.get(
                kind, one_line(kind, 20) or "Unknown")),
            DetailRow("Architecture", "x86_64 (64-bit)" if detected == "x86_64" else
                      ("Not stated by the installer" if detected is None else architecture_label(detected))),
            DetailRow("Size", format_size(installer.get("size"))),
        ]
        if app:
            status = app.get("status") if app.get("status") in CATALOG_LABELS else "unknown"
            rows += [
                DetailRow("Application", one_line(app.get("name"), 120) or "Unknown"),
                DetailRow("Application ID", one_line(app.get("id"), 96) or "Unknown"),
                DetailRow("Version", one_line(app.get("version"), 40) or "Unknown"),
                DetailRow("Catalog status", CATALOG_LABELS[status]),
                DetailRow("Source", source_label(app.get("manifest_source"))),
            ]
        if installer.get("sha256"):
            rows.append(DetailRow("SHA-256", one_line(installer.get("sha256"), 64)))
        return rows

    def sandbox_rows(self) -> list[PermissionRow]:
        return sandbox_rows(self.inspection.get("sandbox"))

    @property
    def refusal_title(self) -> str:
        if self.state == self.REFUSED_32BIT:
            return UNSUPPORTED_32BIT_MESSAGE
        return REFUSAL_TITLES.get(self.error_code or "", "This installer cannot be installed")

    @property
    def refusal_detail(self) -> str:
        if self.state == self.REFUSED_32BIT:
            return UNSUPPORTED_32BIT_DETAIL
        return self.error_message

    @property
    def refusal_hint(self) -> str:
        return "" if self.state == self.REFUSED_32BIT else REFUSAL_HINTS.get(self.error_code or "", "")

    @property
    def confirm_title(self) -> str:
        if self.upgrading:
            return f"Update {self.upgrade_name or self.application_name}"
        return f"Install {self.application_name}"

    @property
    def confirm_label(self) -> str:
        return "Update" if self.upgrading else "Install"

    @property
    def installing_title(self) -> str:
        verb = "Updating" if self.upgrading else "Installing"
        return f"{verb} {self.upgrade_name or self.application_name}…"

    @property
    def done_title(self) -> str:
        app = _dict(self.result.get("application"))
        name = one_line(app.get("name"), 120) or self.upgrade_name or self.application_name
        return f"{name} was updated." if self.upgrading else f"{name} is installed."

    @property
    def can_launch(self) -> bool:
        return self.state == self.DONE and bool(_dict(self.result.get("application")).get("launch"))

    @property
    def done_note(self) -> str:
        if self.state != self.DONE or self.can_launch:
            return ""
        return ("Boswas could not tell which program starts this application. Open Applications and use Details to "
                "see the programs it found.")

    @property
    def failed_title(self) -> str:
        if self.failed_phase == "check":
            return "The installer could not be checked"
        return "The update failed" if self.upgrading else "The installation failed"
