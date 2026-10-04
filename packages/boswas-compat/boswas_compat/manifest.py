"""Compatibility manifests, format version 1.

The normative schema is compatibility/manifests/schema/manifest-v1.schema.json
(installed as /usr/share/boswas/compat/schema/). This module implements the
same rules with the standard library; a unit test keeps both in sync.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import MANIFEST_SCHEMA_VERSION
from .errors import ManifestError

STATUSES = ("unknown", "untested", "experimental", "tested", "approved", "blocked")
# Statuses that claim Boswas validation: the manifest must pin the installer.
PINNED_STATUSES = ("tested", "approved")
ARCHITECTURES = ("x86_64", "x86")
INSTALLER_TYPES = ("exe", "msi", "portable")
FOLDERS = ("documents", "downloads", "desktop", "pictures", "music", "videos")
SANDBOX_FLAGS = ("network", "display", "audio", "gpu")

TOP_LEVEL_KEYS = {
    "$schema", "schemaVersion", "id", "name", "version", "publisher", "description",
    "runtime", "architecture", "dependencies", "environment", "winetricks", "launch",
    "arguments", "status", "installer", "sandbox", "notes", "tested",
}
REQUIRED_KEYS = ("id", "name", "version", "runtime", "architecture", "launch", "status")

ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
ID_MAX = 96
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINE_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
DEPENDENCY_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")
WINETRICKS_RE = re.compile(r"^[a-z0-9_]+(?:=[a-z0-9_.]+)?$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
FILE_NAME_RE = re.compile(r'^[^\\/:*?"<>|\x00-\x1f]{1,255}$')
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Environment the sandbox controls; a manifest may never set these.
ENV_DENIED = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "DISPLAY", "WAYLAND_DISPLAY",
    "XAUTHORITY", "WINEPREFIX", "WINESERVER", "WINELOADER", "WINEARCH", "WINEDLLPATH",
})
ENV_DENIED_PREFIXES = ("LD_", "XDG_", "DBUS_", "PULSE_", "BOSWAS_", "PYTHON", "GIO_", "GTK_", "QT_")

_WIN_COMPONENT_RE = re.compile(r'^[^<>:"|?*\x00-\x1f]+$')


@dataclass(frozen=True)
class Installer:
    type: str | None = None
    sha256: str | None = None
    silent_args: tuple[str, ...] = ()
    file_name: str | None = None


@dataclass(frozen=True)
class Sandbox:
    network: bool = False
    display: bool = True
    audio: bool = False
    gpu: bool = False
    folders: tuple[str, ...] = ()


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    version: str
    status: str
    architecture: str
    wine_version: str
    launch: str
    publisher: str | None = None
    description: str | None = None
    arguments: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    winetricks: tuple[str, ...] = ()
    installer: Installer = field(default_factory=Installer)
    sandbox: Sandbox = field(default_factory=Sandbox)
    notes: str | None = None
    # Where it was loaded from (not part of the document).
    layer: str = "inline"
    path: str | None = None
    digest: str | None = None

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "publisher": self.publisher,
            "status": self.status,
            "architecture": self.architecture,
            "layer": self.layer,
            "installer_pinned": bool(self.installer.sha256),
            "sandbox": {
                "network": self.sandbox.network,
                "display": self.sandbox.display,
                "audio": self.sandbox.audio,
                "gpu": self.sandbox.gpu,
                "folders": list(self.sandbox.folders),
            },
        }


def valid_id(value: object) -> bool:
    return isinstance(value, str) and len(value) <= ID_MAX and bool(ID_RE.fullmatch(value))


def env_key_allowed(key: str) -> bool:
    return (bool(ENV_KEY_RE.fullmatch(key)) and key not in ENV_DENIED
            and not key.startswith(ENV_DENIED_PREFIXES))


def windows_path_problem(value: str) -> str | None:
    """Why a launch path is unacceptable, or None.

    Accepted: "C:\\dir\\app.exe", "dir/app.exe" (relative to C:\\) or a bare
    file name ("app.exe", searched under C:\\). Never another drive, a UNC
    path or "..".
    """
    if not value or len(value) > 260:
        return "must be 1-260 characters"
    path = value.replace("/", "\\")
    if path.startswith("\\\\"):
        return "UNC paths are not allowed"
    if len(path) >= 2 and path[1] == ":":
        if path[0] not in "cC":
            return "only drive C: is allowed"
        path = path[2:]
    parts = [p for p in path.split("\\") if p]
    if not parts:
        return "no file name"
    for part in parts:
        if part in (".", ".."):
            return "'.' and '..' are not allowed"
        if not _WIN_COMPONENT_RE.fullmatch(part):
            return f"invalid characters in {part!r}"
    if not parts[-1].lower().endswith((".exe", ".bat", ".cmd", ".msc", ".com")):
        return "must name a Windows program (.exe, .com, .bat, .cmd, .msc)"
    return None


def _string_list(doc: dict, key: str, problems: list[str], pattern: re.Pattern | None = None) -> None:
    value = doc.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{key}: must be a list of strings")
        return
    if len(set(value)) != len(value):
        problems.append(f"{key}: duplicate entries")
    if pattern:
        for item in value:
            if not pattern.fullmatch(item):
                problems.append(f"{key}: invalid entry {item!r}")


def validate(doc: object) -> list[str]:
    """All problems with a manifest document (empty list: valid)."""
    if not isinstance(doc, dict):
        return ["manifest must be a JSON object"]
    problems: list[str] = []
    for key in sorted(set(doc) - TOP_LEVEL_KEYS):
        problems.append(f"unknown key {key!r}")
    for key in REQUIRED_KEYS:
        if key not in doc:
            problems.append(f"missing required key {key!r}")
    if "schemaVersion" in doc and doc["schemaVersion"] != MANIFEST_SCHEMA_VERSION:
        problems.append(f"schemaVersion: only {MANIFEST_SCHEMA_VERSION} is supported")

    if "id" in doc and not valid_id(doc["id"]):
        problems.append("id: lower-case reverse-DNS name with at least one dot, e.g. 'com.example.app' (max 96)")
    for key, limit in (("name", 80), ("version", 40), ("publisher", 80), ("description", 500), ("notes", 2000)):
        if key in doc:
            value = doc[key]
            required_nonempty = key in ("name", "version")
            if not isinstance(value, str) or len(value) > limit or (required_nonempty and not value.strip()):
                problems.append(f"{key}: must be a {'non-empty ' if required_nonempty else ''}string of at most {limit} characters")
            else:
                multiline = key in ("description", "notes")
                if any(ord(c) < 32 and not (multiline and c in "\n\t") for c in value):
                    problems.append(f"{key}: control characters are not allowed")

    runtime = doc.get("runtime")
    if "runtime" in doc:
        if not isinstance(runtime, dict) or set(runtime) - {"type", "wineVersion"}:
            problems.append("runtime: object with 'type' and 'wineVersion' only")
        else:
            if runtime.get("type") != "wine":
                problems.append("runtime.type: must be 'wine'")
            if not isinstance(runtime.get("wineVersion"), str) or not WINE_VERSION_RE.fullmatch(runtime["wineVersion"]):
                problems.append("runtime.wineVersion: e.g. '10' or '10.0'")

    if "architecture" in doc and doc["architecture"] not in ARCHITECTURES:
        problems.append(f"architecture: one of {', '.join(ARCHITECTURES)}")
    if "status" in doc and doc["status"] not in STATUSES:
        problems.append(f"status: one of {', '.join(STATUSES)}")

    if "dependencies" in doc:
        _string_list(doc, "dependencies", problems, DEPENDENCY_RE)
    if "winetricks" in doc:
        _string_list(doc, "winetricks", problems, WINETRICKS_RE)
    if "arguments" in doc:
        _string_list(doc, "arguments", problems)

    env = doc.get("environment", {})
    if not isinstance(env, dict):
        problems.append("environment: must be an object of strings")
    else:
        for key, value in env.items():
            if not env_key_allowed(key):
                problems.append(f"environment: {key!r} is not allowed (controlled by the sandbox)")
            if not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 for c in value):
                problems.append(f"environment: value of {key!r} must be a single-line string (max 1024)")
            elif key == "WINEDLLOVERRIDES" and "winemenubuilder" in value.lower():
                problems.append("environment: WINEDLLOVERRIDES may not change winemenubuilder")

    if "launch" in doc:
        if not isinstance(doc["launch"], str):
            problems.append("launch: must be a string")
        else:
            why = windows_path_problem(doc["launch"])
            if why:
                problems.append(f"launch: {why}")

    installer = doc.get("installer", {})
    if not isinstance(installer, dict):
        problems.append("installer: must be an object")
        installer = {}
    for key in sorted(set(installer) - {"type", "sha256", "silentArgs", "fileName"}):
        problems.append(f"installer: unknown key {key!r}")
    if "type" in installer and installer["type"] not in INSTALLER_TYPES:
        problems.append(f"installer.type: one of {', '.join(INSTALLER_TYPES)}")
    if "sha256" in installer and (not isinstance(installer["sha256"], str) or not SHA256_RE.fullmatch(installer["sha256"])):
        problems.append("installer.sha256: 64 lower-case hex characters")
    if "silentArgs" in installer:
        _string_list(installer, "silentArgs", problems)
    if "fileName" in installer and (not isinstance(installer["fileName"], str) or not FILE_NAME_RE.fullmatch(installer["fileName"])):
        problems.append("installer.fileName: a plain file name")
    if doc.get("status") in PINNED_STATUSES and "sha256" not in installer:
        problems.append(f"installer.sha256: required for status {doc.get('status')!r} (the validated installer must be pinned)")

    sandbox = doc.get("sandbox", {})
    if not isinstance(sandbox, dict):
        problems.append("sandbox: must be an object")
        sandbox = {}
    for key in sorted(set(sandbox) - set(SANDBOX_FLAGS) - {"folders"}):
        problems.append(f"sandbox: unknown key {key!r}")
    for key in SANDBOX_FLAGS:
        if key in sandbox and not isinstance(sandbox[key], bool):
            problems.append(f"sandbox.{key}: must be true or false")
    if "folders" in sandbox:
        folders = sandbox["folders"]
        if not isinstance(folders, list) or not all(f in FOLDERS for f in folders) or len(set(folders)) != len(folders):
            problems.append(f"sandbox.folders: unique entries from {', '.join(FOLDERS)}")

    tested = doc.get("tested", {})
    if not isinstance(tested, dict) or set(tested) - {"date", "osVersion", "by"}:
        problems.append("tested: object with 'date', 'osVersion', 'by'")
    elif "date" in tested and (not isinstance(tested["date"], str) or not DATE_RE.fullmatch(tested["date"])):
        problems.append("tested.date: YYYY-MM-DD")
    return problems


def from_document(doc: dict, *, layer: str = "inline", path: str | None = None, digest: str | None = None) -> Manifest:
    problems = validate(doc)
    if problems:
        raise ManifestError(f"invalid manifest{' ' + path if path else ''}: {problems[0]}", problems)
    installer = doc.get("installer", {})
    sandbox = doc.get("sandbox", {})
    return Manifest(
        id=doc["id"],
        name=doc["name"].strip(),
        version=doc["version"].strip(),
        status=doc["status"],
        architecture=doc["architecture"],
        wine_version=doc["runtime"]["wineVersion"],
        launch=doc["launch"],
        publisher=doc.get("publisher"),
        description=doc.get("description"),
        arguments=tuple(doc.get("arguments", [])),
        dependencies=tuple(doc.get("dependencies", [])),
        environment=tuple(sorted(doc.get("environment", {}).items())),
        winetricks=tuple(doc.get("winetricks", [])),
        installer=Installer(
            type=installer.get("type"),
            sha256=installer.get("sha256"),
            silent_args=tuple(installer.get("silentArgs", [])),
            file_name=installer.get("fileName"),
        ),
        sandbox=Sandbox(
            network=sandbox.get("network", False),
            display=sandbox.get("display", True),
            audio=sandbox.get("audio", False),
            gpu=sandbox.get("gpu", False),
            folders=tuple(sandbox.get("folders", [])),
        ),
        notes=doc.get("notes"),
        layer=layer,
        path=path,
        digest=digest,
    )


MAX_MANIFEST_BYTES = 256 * 1024


def load(path: Path, layer: str = "file") -> Manifest:
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc.strerror}") from exc
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError(f"{path}: larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"{path}: not valid JSON ({exc})") from exc
    manifest = from_document(doc, layer=layer, path=str(path), digest=hashlib.sha256(data).hexdigest())
    if path.name != f"{manifest.id}.json":
        raise ManifestError(f"{path}: file name must be {manifest.id}.json")
    return manifest
