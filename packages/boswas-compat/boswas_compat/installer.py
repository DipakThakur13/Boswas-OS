"""Installer inspection: file type, Windows architecture, installer framework.

Detection uses file signatures, never the file name. Only headers, signature
strings and (for Windows Installer packages) the summary information stream
are read; installer content is never executed outside the sandbox.
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


# --- Windows Installer packages: platform from the summary information ---------------
#
# An .msi file is an OLE compound file. Its "\x05SummaryInformation" stream
# holds the Template property (PID 7), "<platform>;<languages>", e.g.
# "x64;1033" or "Intel;1033". Only that stream is read, with every offset,
# chain and count bounded, so a malformed file yields "unknown", never a hang.

ENDOFCHAIN, FREESECT = 0xFFFFFFFE, 0xFFFFFFFF
MAX_CFB_ENTRIES = 100_000
MAX_PROPERTY_STREAM = 1024 * 1024
PID_TEMPLATE = 7
VT_LPSTR = 0x1E
MSI_PLATFORMS = {"x64": "x86_64", "amd64": "x86_64", "intel": "x86", "": "x86", "intel64": "ia64",
                 "arm64": "arm64", "arm": "arm"}


class _CfbError(Exception):
    pass


class _Cfb:
    def __init__(self, fh, size: int):
        self.fh, self.size = fh, size
        header = self._read_at(0, 512)
        if header[:8] != OLE_MAGIC:
            raise _CfbError("not a compound file")
        shift, mini_shift = struct.unpack_from("<HH", header, 0x1E)
        if shift not in (9, 12) or mini_shift != 6:
            raise _CfbError("unsupported sector size")
        self.sector = 1 << shift
        self.mini_sector = 1 << mini_shift
        self.max_sectors = size // self.sector + 1
        (n_fat, self.dir_start, _, self.mini_cutoff, mini_fat_start, n_mini_fat,
         difat_start, n_difat) = struct.unpack_from("<IIIIIIII", header, 0x2C)
        fat_sectors = [s for s in struct.unpack_from("<109I", header, 0x4C) if s < ENDOFCHAIN]
        seen = 0
        while difat_start < ENDOFCHAIN and seen < n_difat and seen < self.max_sectors:
            block = self._sector(difat_start)
            entries = struct.unpack_from(f"<{self.sector // 4}I", block)
            fat_sectors += [s for s in entries[:-1] if s < ENDOFCHAIN]
            difat_start, seen = entries[-1], seen + 1
        fat_sectors = fat_sectors[:n_fat]
        self.fat = b"".join(self._sector(s) for s in fat_sectors)
        self.mini_fat = self._chain_bytes(mini_fat_start, self.fat) if n_mini_fat else b""

    def _read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset + length > self.size:
            raise _CfbError("offset outside the file")
        self.fh.seek(offset)
        data = self.fh.read(length)
        if len(data) != length:
            raise _CfbError("short read")
        return data

    def _sector(self, n: int) -> bytes:
        return self._read_at((n + 1) * self.sector, self.sector)

    @staticmethod
    def _next(table: bytes, n: int) -> int:
        if (n + 1) * 4 > len(table):
            raise _CfbError("chain leaves the allocation table")
        return struct.unpack_from("<I", table, n * 4)[0]

    def _chain(self, start: int, table: bytes) -> list[int]:
        chain, n = [], start
        while n < ENDOFCHAIN:
            chain.append(n)
            if len(chain) > self.max_sectors:
                raise _CfbError("allocation chain loops")
            n = self._next(table, n)
        return chain

    def _chain_bytes(self, start: int, table: bytes) -> bytes:
        return b"".join(self._sector(n) for n in self._chain(start, table))

    def read_stream(self, name: str, limit: int) -> bytes | None:
        directory = self._chain_bytes(self.dir_start, self.fat)
        entries = [directory[i:i + 128] for i in range(0, min(len(directory), MAX_CFB_ENTRIES * 128), 128)]
        if not entries:
            return None
        root = entries[0]
        for entry in entries:
            name_len = struct.unpack_from("<H", entry, 0x40)[0]
            if entry[0x42] != 2 or not 2 <= name_len <= 64:
                continue
            if entry[:name_len - 2].decode("utf-16-le", errors="replace") != name:
                continue
            start = struct.unpack_from("<I", entry, 0x74)[0]
            size = struct.unpack_from("<I", entry, 0x78)[0]
            if size > limit:
                raise _CfbError("stream too large")
            if size < self.mini_cutoff:
                ministream = self._chain_bytes(struct.unpack_from("<I", root, 0x74)[0], self.fat)
                data = b"".join(ministream[n * self.mini_sector:(n + 1) * self.mini_sector]
                                for n in self._chain(start, self.mini_fat))
            else:
                data = self._chain_bytes(start, self.fat)
            if len(data) < size:
                raise _CfbError("stream shorter than its size")
            return data[:size]
        return None


def _summary_template(stream: bytes) -> str | None:
    """Template property (PID 7) of an OLE property set stream."""
    if len(stream) < 48 or stream[:2] != b"\xfe\xff":
        return None
    (offset,) = struct.unpack_from("<I", stream, 44)
    if offset + 8 > len(stream):
        return None
    _size, count = struct.unpack_from("<II", stream, offset)
    for i in range(min(count, 1024)):
        pos = offset + 8 + 8 * i
        if pos + 8 > len(stream):
            return None
        pid, value_offset = struct.unpack_from("<II", stream, pos)
        if pid != PID_TEMPLATE:
            continue
        value = offset + value_offset
        if value + 8 > len(stream):
            return None
        vtype, length = struct.unpack_from("<HxxI", stream, value)
        if vtype != VT_LPSTR or value + 8 + length > len(stream):
            return None
        return stream[value + 8:value + 8 + length].split(b"\0", 1)[0].decode("latin-1")
    return None


def msi_platform(path: Path) -> str | None:
    """Windows architecture of an .msi package from its Template property, or None if unknown."""
    try:
        with path.open("rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            stream = _Cfb(fh, size).read_stream("\x05SummaryInformation", MAX_PROPERTY_STREAM)
    except (OSError, struct.error, _CfbError):
        return None
    template = _summary_template(stream) if stream else None
    if template is None:
        return None
    platform = template.split(";", 1)[0].split(",", 1)[0].strip().lower()
    return MSI_PLATFORMS.get(platform, f"unknown({platform[:16]})")


def inspect(path: Path) -> InstallerInfo:
    try:
        with path.open("rb") as fh:
            head = fh.read(HEADER_BYTES)
    except OSError as exc:
        raise NotFound(f"cannot read installer {path}: {exc.strerror}") from exc
    frameworks, _ = load_installer_types()
    if head.startswith(OLE_MAGIC):
        return InstallerInfo(kind="msi", machine=msi_platform(path), subsystem=None, framework="Windows Installer")
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


def hash_file(src: Path, max_bytes: int) -> tuple[str, int]:
    """SHA-256 and size of an installer without copying it (boswas-winapp inspect)."""
    digest = hashlib.sha256()
    size = 0
    try:
        fd = os.open(src, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        raise NotFound(f"cannot open installer {src}: {exc.strerror}") from exc
    try:
        if not _is_regular(fd):
            raise Refused(f"{src} is not a regular file", reason="bad-installer")
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise Refused(f"installer is larger than the policy limit ({max_bytes // (1024 * 1024)} MiB)",
                              reason="too-large")
            digest.update(chunk)
    finally:
        os.close(fd)
    if size == 0:
        raise Refused(f"{src} is empty", reason="bad-installer")
    return digest.hexdigest(), size


def _is_regular(fd: int) -> bool:
    return stat.S_ISREG(os.fstat(fd).st_mode)
