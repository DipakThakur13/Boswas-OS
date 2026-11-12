"""Parsers for Waydroid's command output and files (Waydroid 1.6, read from its source).

`waydroid status` prints tab-separated "Key:\tvalue" lines (Session, Container,
Vendor type, IP address, Session user, Wayland display). `waydroid app list`
prints blocks of "Name: …", "packageName: …", "categories:" and tab-indented
categories. Most commands report failures only as log lines (exit status 0),
e.g. "WayDroid session is stopped"; uninitialised runtimes answer
'Waydroid is not initialized, run "waydroid init"'. Everything is parsed
defensively: unknown lines are ignored.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

NOT_INITIALIZED = "waydroid is not initialized"
SESSION_STOPPED = "session is stopped"
ERROR_MARKERS = ("session is stopped", "failed to", "sending reply failed", "not initialized", "error:",
                 "doesn't exist", "is not set")


def parse_status(text: str) -> dict:
    values: dict[str, str] = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip().lower()] = value.strip()
    session = values.get("session", "").upper()
    return {
        "initialized": NOT_INITIALIZED not in (text or "").lower(),
        "running": session == "RUNNING",
        "state": values.get("container") or (session or None) or None,
        "ip": values.get("ip address") if values.get("ip address") not in (None, "", "UNKNOWN") else None,
        "vendor": values.get("vendor type"),
    }


def parse_app_list(text: str) -> list[dict]:
    apps: list[dict] = []
    current: dict | None = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if line.startswith("Name: "):
            current = {"package": None, "name": line[len("Name: "):].strip(), "version": None, "categories": []}
            apps.append(current)
        elif line.startswith("packageName: ") and current is not None:
            current["package"] = line[len("packageName: "):].strip()
        elif line.startswith("\t") and current is not None and stripped:
            current["categories"].append(stripped)
    return [a for a in apps if a["package"]]


def output_error(text: str) -> str | None:
    """The first line of Waydroid output that reports a failure, if any."""
    for line in (text or "").splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in ERROR_MARKERS):
            return re.sub(r"^\[[^\]]*\]\s*", "", line.strip())[:300]
    return None


def image_version(value: str | None) -> str | None:
    """Waydroid stores the OTA build time of each image as seconds since the epoch ("0" = none)."""
    try:
        seconds = int(value or "0")
    except ValueError:
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def dpkg_version(status_text: str | None, package: str) -> str | None:
    """Installed version of package from /var/lib/dpkg/status, or None."""
    if not status_text:
        return None
    for stanza in status_text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in stanza.splitlines():
            if line and not line[0].isspace():
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        if fields.get("Package") == package and fields.get("Status", "").endswith(" installed") \
                and fields.get("Status", "").startswith("install "):
            return fields.get("Version")
    return None


def bridge_address(ip_json: str) -> str | None:
    """IPv4 address of the bridge from `ip -j -4 addr show dev waydroid0`."""
    try:
        doc = json.loads(ip_json or "[]")
    except ValueError:
        return None
    for link in doc if isinstance(doc, list) else []:
        for info in (link.get("addr_info") or []) if isinstance(link, dict) else []:
            if isinstance(info, dict) and info.get("family") == "inet" and isinstance(info.get("local"), str):
                return info["local"]
    return None
