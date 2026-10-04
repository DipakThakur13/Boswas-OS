"""Installer inspection: file type, Windows architecture, installer framework.

Detection uses file signatures, never the file name. Only headers and
signature strings are read; installer content is never executed outside the
sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .errors import NotFound, Refused

MZ = b"MZ"
OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
PE_MACHINES = {0x8664: "x86_64", 0x014C: "x86", 0xAA64: "arm64"}
PE_SUBSYSTEMS = {2: "gui", 3: "console"}
FRAMEWORK_SCAN_BYTES = 16 * 1024 * 1024
HEADER_BYTES = 64 * 1024
_CHUNK = 1024 * 1024

DEFAULT_FRAMEWORKS = (
    {"name": "NSIS", "signature": "Nullsoft Install System", "silentArgs": ["/S"]},
    {"name": "Inno Setup", "signature": "Inno Setup Setup Data",
     "silentArgs": ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]},
)
DEFAULT_MSI_SILENT = ("/qn", "/norestart")


@dataclass(frozen=True)
class InstallerInfo:
    kind: str                      # exe | msi
    machine: str | None            # x86_64 | x86 | arm64 | unknown (PE only)
    subsystem: str | None          # gui | console (PE only)
    framework: str | None          # NSIS, Inno Setup, ...
    framework_silent_args: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict:
        return {"kind": self.kind, "machine": self.machine, "subsystem": self.subsystem,
                "framework": self.framework}


def load_installer_types() -> tuple[tuple[dict, ...], tuple[str, ...]]:
    """Framework signatures and MSI silent arguments from installers.json."""
    try:
        data = json.loads(paths.system_path(paths.INSTALLERS_FILE).read_text(encoding="utf-8"))
        frameworks = tuple(f for f in data.get("frameworks", [])
                           if isinstance(f.get("name"), str) and isinstance(f.get("signature"), str))
        msi = tuple(data.get("types", {}).get("msi", {}).get("silentArgs", DEFAULT_MSI_SILENT))
        return frameworks or DEFAULT_FRAMEWORKS, msi
    except (OSError, ValueError, AttributeError):
        return DEFAULT_FRAMEWORKS, DEFAULT_MSI_SILENT


def _pe_header(head: bytes) -> tuple[str, str | None]:
    if len(head) < 0x40:
        raise Refused("not a Windows program: truncated DOS header", reason="bad-installer")
    (pe_offset,) = struct.unpack_from("<I", head, 0x3C)
    if pe_offset + 24 + 70 > len(head) or head[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise Refused("not a Windows program: no PE header (DOS or damaged executable)", reason="bad-installer")
    (machine,) = struct.unpack_from("<H", head, pe_offset + 4)
    optional = pe_offset + 24
    (magic,) = struct.unpack_from("<H", head, optional)
    subsystem = None
    if magic in (0x10B, 0x20B):
        (sub,) = struct.unpack_from("<H", head, optional + 68)
        subsystem = PE_SUBSYSTEMS.get(sub, f"other({sub})")
    return PE_MACHINES.get(machine, f"unknown(0x{machine:04x})"), subsystem


def _framework(path: Path, frameworks) -> dict | None:
    needles = [(f, f["signature"].encode()) for f in frameworks]
    longest = max((len(n) for _, n in needles), default=0)
    tail = b""
    read = 0
    with path.open("rb") as fh:
        while read < FRAMEWORK_SCAN_BYTES:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            read += len(chunk)
            window = tail + chunk
            for fw, needle in needles:
                if needle in window:
                    return fw
            tail = window[-longest:] if longest else b""
    return None


def inspect(path: Path) -> InstallerInfo:
    try:
        with path.open("rb") as fh:
            head = fh.read(HEADER_BYTES)
    except OSError as exc:
        raise NotFound(f"cannot read installer {path}: {exc.strerror}") from exc
    frameworks, _ = load_installer_types()
    if head.startswith(OLE_MAGIC):
        return InstallerInfo(kind="msi", machine=None, subsystem=None, framework="Windows Installer")
    if head.startswith(MZ):
        machine, subsystem = _pe_header(head)
        fw = _framework(path, frameworks)
        return InstallerInfo(kind="exe", machine=machine, subsystem=subsystem,
                             framework=fw["name"] if fw else None,
                             framework_silent_args=tuple(fw.get("silentArgs", [])) if fw else ())
    raise Refused("not a Windows installer: expected a PE executable (.exe) or a Windows Installer "
                  "package (.msi)", reason="bad-installer")


def copy_and_hash(src: Path, dest: Path, max_bytes: int) -> tuple[str, int]:
    """Copy src to dest (new file, mode 0600) and return (sha256, size).

    The hash is computed over the bytes written, so the checked file is the
    file that is later used, whatever happens to the source meanwhile.
    """
    digest = hashlib.sha256()
    size = 0
    try:
        src_fd = os.open(src, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as exc:
        raise NotFound(f"cannot open installer {src}: {exc.strerror}") from exc
    try:
        if not _is_regular(src_fd):
            raise Refused(f"{src} is not a regular file", reason="bad-installer")
        dest_fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            while True:
                chunk = os.read(src_fd, _CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise Refused(f"installer is larger than the policy limit ({max_bytes // (1024 * 1024)} MiB)",
                                  reason="too-large")
                digest.update(chunk)
                os.write(dest_fd, chunk)
        finally:
            os.close(dest_fd)
    finally:
        os.close(src_fd)
    if size == 0:
        raise Refused(f"{src} is empty", reason="bad-installer")
    return digest.hexdigest(), size


def _is_regular(fd: int) -> bool:
    return stat.S_ISREG(os.fstat(fd).st_mode)
