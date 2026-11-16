"""Shared helpers of the Archive Manager tests: hand-built archives and a stand-in for the 7z program.

Hostile archives are written byte by byte (ZIP) or member by member (TAR),
so a test controls every field: names with ``..`` or NUL, sizes that lie,
link and device entries, legacy encryption. Nothing here needs root, the
network or a program that is not part of Python.
"""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import struct
import sys
import tarfile
import time
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))                      # packages/boswas-apps
sys.path.insert(0, str(HERE.parents[3] / "desktop" / "ui" / "qt"))

try:
    import PySide6  # noqa: F401
    PYSIDE = True
except ImportError:                                           # the engine tests run without it
    PYSIDE = False

DOS_TIME = (2024, 5, 17, 10, 30, 12)


def _dos(stamp: tuple) -> tuple[int, int]:
    year, month, day, hour, minute, second = stamp
    return (hour << 11) | (minute << 5) | (second // 2), ((year - 1980) << 9) | (month << 5) | day


# --- ZipCrypto (the legacy encryption of ZIP) --------------------------------------------------------------

class _Keys:
    def __init__(self, password: bytes) -> None:
        self.k0, self.k1, self.k2 = 0x12345678, 0x23456789, 0x34567890
        for byte in password:
            self.update(byte)

    def update(self, byte: int) -> None:
        self.k0 = zlib.crc32(bytes([byte]), self.k0 ^ 0xFFFFFFFF) ^ 0xFFFFFFFF
        self.k1 = (self.k1 + (self.k0 & 0xFF)) & 0xFFFFFFFF
        self.k1 = (self.k1 * 134775813 + 1) & 0xFFFFFFFF
        self.k2 = zlib.crc32(bytes([self.k1 >> 24]), self.k2 ^ 0xFFFFFFFF) ^ 0xFFFFFFFF

    def stream_byte(self) -> int:
        temp = (self.k2 | 2) & 0xFFFF
        return ((temp * (temp ^ 1)) >> 8) & 0xFF


def zipcrypto(data: bytes, password: str, crc: int) -> bytes:
    """``data`` encrypted the way ZIP did it before AES, with the 12-byte header that carries the check byte."""
    keys = _Keys(password.encode("utf-8"))
    header = bytes(range(1, 12)) + bytes([(crc >> 24) & 0xFF])
    out = bytearray()
    for byte in header + data:
        out.append(byte ^ keys.stream_byte())
        keys.update(byte)
    return bytes(out)


# --- ZIP, byte by byte -------------------------------------------------------------------------------------

def zip_member(name, data: bytes = b"", *, method: int = 8, utf8: bool | None = None, mode: int | None = None,
               system: int = 3, password: str | None = None, claim_size: int | None = None, extra: bytes = b"",
               stamp: tuple = DOS_TIME, crc: int | None = None, is_dir: bool = False) -> dict:
    """The description of one entry for ``raw_zip``. ``name`` is text (UTF-8) or the raw bytes to store."""
    if isinstance(name, str):
        raw_name = name.encode("utf-8")
        flag_utf8 = (not raw_name.isascii()) if utf8 is None else utf8
    else:
        raw_name = bytes(name)
        flag_utf8 = bool(utf8)
    return {"name": raw_name, "data": data, "method": method, "utf8": flag_utf8, "mode": mode, "system": system,
            "password": password, "claim_size": claim_size, "extra": extra, "stamp": stamp, "crc": crc,
            "is_dir": is_dir}


def raw_zip(path, members: list[dict], comment: bytes = b"") -> Path:
    """Write a ZIP file from ``zip_member`` descriptions, exactly as described (nothing is validated)."""
    body = io.BytesIO()
    central = io.BytesIO()
    for member in members:
        data = member["data"]
        checksum = zlib.crc32(data) & 0xFFFFFFFF if member["crc"] is None else member["crc"]
        if member["method"] == 8:
            compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
            packed = compressor.compress(data) + compressor.flush()
        else:
            packed = data
        flags = 0x800 if member["utf8"] else 0
        if member["password"] is not None:
            flags |= 0x1
            packed = zipcrypto(packed, member["password"], checksum)
        size = len(data) if member["claim_size"] is None else member["claim_size"]
        dos_time, dos_date = _dos(member["stamp"])
        mode = member["mode"]
        if mode is None:
            mode = (stat.S_IFDIR | 0o755) if member["is_dir"] else (stat.S_IFREG | 0o644)
        external = (mode << 16) if member["system"] == 3 else (0x10 if member["is_dir"] else 0x20)
        offset = body.tell()
        name, extra = member["name"], member["extra"]
        body.write(struct.pack("<4sHHHHHIIIHH", b"PK\x03\x04", 20, flags, member["method"], dos_time, dos_date,
                               checksum, len(packed), size, len(name), len(extra)))
        body.write(name + extra + packed)
        central.write(struct.pack("<4sBBHHHHHIIIHHHHHII", b"PK\x01\x02", 20, member["system"], 20, flags,
                                  member["method"], dos_time, dos_date, checksum, len(packed), size, len(name),
                                  len(extra), 0, 0, 0, external & 0xFFFFFFFF, offset))
        central.write(name + extra)
    directory = central.getvalue()
    start = body.tell()
    count = len(members)
    body.write(directory)
    if count > 0xFFFE or start > 0xFFFFFFFE:
        zip64_offset = body.tell()
        body.write(struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, len(directory), start))
        body.write(struct.pack("<4sIQI", b"PK\x06\x07", 0, zip64_offset, 1))
    body.write(struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, min(count, 0xFFFF), min(count, 0xFFFF),
                           min(len(directory), 0xFFFFFFFF), min(start, 0xFFFFFFFF), len(comment)))
    body.write(comment)
    target = Path(path)
    target.write_bytes(body.getvalue())
    return target


def unicode_path_extra(raw_name: bytes, text: str) -> bytes:
    """The Info-ZIP Unicode Path extra field that names ``text`` for the stored ``raw_name``."""
    payload = b"\x01" + struct.pack("<I", zlib.crc32(raw_name) & 0xFFFFFFFF) + text.encode("utf-8")
    return struct.pack("<HH", 0x7075, len(payload)) + payload


# --- TAR, member by member ---------------------------------------------------------------------------------

def tar_member(name: str, data: bytes | None = b"", *, kind: bytes = tarfile.REGTYPE, link: str = "",
               mode: int = 0o644, mtime: float = 1_700_000_000, pax: dict | None = None) -> tuple:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.mtime = mtime
    info.linkname = link
    if pax:
        info.pax_headers = dict(pax)
    if kind in (tarfile.CHRTYPE, tarfile.BLKTYPE):
        info.devmajor, info.devminor = 1, 3
    payload = data if kind == tarfile.REGTYPE else None
    info.size = len(payload) if payload is not None else 0
    return info, payload


def raw_tar(path, members: list[tuple], mode: str = "w", fmt: int = tarfile.GNU_FORMAT) -> Path:
    """Write a TAR file from ``tar_member`` descriptions. ``mode``: ``w``, ``w:gz``, ``w:bz2``, ``w:xz``."""
    with tarfile.open(path, mode, format=fmt, errors="surrogateescape") as archive:
        for info, payload in members:
            archive.addfile(info, io.BytesIO(payload) if payload is not None else None)
    return Path(path)


def tar_dir(name: str, mode: int = 0o755, mtime: float = 1_700_000_000) -> tuple:
    return tar_member(name, None, kind=tarfile.DIRTYPE, mode=mode, mtime=mtime)


def tar_link(name: str, target: str, hard: bool = False) -> tuple:
    return tar_member(name, None, kind=tarfile.LNKTYPE if hard else tarfile.SYMTYPE, link=target, mode=0o777)


# --- looking at what extraction did ------------------------------------------------------------------------

def tree(root) -> dict[str, str]:
    """Everything below ``root``: relative path → ``dir``, ``link:<target>`` or ``file:<size>``. Links are not followed."""
    found: dict[str, str] = {}
    root = os.fspath(root)
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in folders + files:
            full = os.path.join(directory, name)
            relative = os.path.relpath(full, root)
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode):
                found[relative] = "link:" + os.readlink(full)
            elif stat.S_ISDIR(info.st_mode):
                found[relative] = "dir"
            else:
                found[relative] = f"file:{info.st_size}"
    return found


class Recorder:
    """An extraction handler that records what it is asked and answers from scripted values."""

    def __init__(self, overwrite="skip", confirm=False, passwords=()) -> None:
        self.overwrite_answer = overwrite
        self.confirm_answer = confirm
        self.passwords = list(passwords)
        self.conflicts = []
        self.warnings = []
        self.password_calls = []
        self.progress_calls = []
        self.on_progress = None

    def progress(self, info) -> None:
        self.progress_calls.append(info)
        if self.on_progress is not None:
            self.on_progress(info)

    def overwrite(self, conflict) -> str:
        self.conflicts.append(conflict)
        answer = self.overwrite_answer
        return answer.pop(0) if isinstance(answer, list) else answer

    def confirm_size(self, warning) -> bool:
        self.warnings.append(warning)
        answer = self.confirm_answer
        return answer.pop(0) if isinstance(answer, list) else answer

    def password(self, wrong: bool):
        self.password_calls.append(wrong)
        return self.passwords.pop(0) if self.passwords else None


# --- a stand-in for 7z -------------------------------------------------------------------------------------

SEVEN_MAGIC = b"7z\xbc\xaf\x27\x1c"

_FAKE_7Z = r'''#!/usr/bin/python3 -IB
"""A stand-in for the 7z program in tests: understands "l -slt", "x -so" and "t" on a JSON test archive."""
import base64, json, sys, zlib

MAGIC = b"7z\xbc\xaf\x27\x1c"
args = sys.argv[1:]
command = args[0]
switches = [a for a in args[1:args.index("--")]]
path = args[args.index("--") + 1]
with open("/proc/self/cmdline", "rb") as handle:
    assert b"-p" not in [part[:2] for part in handle.read().split(b"\0")[2:]], "password on the command line"
raw = open(path, "rb").read()
spec = json.loads(raw[len(MAGIC):].decode("utf-8"))
password = sys.stdin.readline().rstrip("\n")
log = spec.get("log")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps(args) + "\n")

def fail(text, code=2):
    sys.stderr.write(text + "\n")
    sys.exit(code)

need = spec.get("password")
if command == "l":
    if spec.get("encrypted_names") and password != need:
        fail("ERROR: " + path + " : Cannot open encrypted archive. Wrong password?")
    out = []
    if spec.get("encrypted_names"):
        out.append("\nEnter password (will not be echoed):")
    for entry in spec["entries"]:
        data = base64.b64decode(entry.get("data", ""))
        lines = ["Path = " + entry["path"], "Size = " + str(entry.get("size", len(data))),
                 "Packed Size = " + str(entry.get("packed", "")), "Modified = " + entry.get("modified", "2024-05-17 10:30:12.0000000"),
                 "Attributes = " + entry.get("attributes", "A -rw-r--r--"),
                 "CRC = " + (entry["crc"] if "crc" in entry else ("%08X" % (zlib.crc32(data) & 0xFFFFFFFF) if data else "")),
                 "Encrypted = " + ("+" if need and data else "-"), "Method = LZMA2:12k", "Block = 0"]
        for key in ("Symbolic Link", "Hard Link", "Folder"):
            if key in entry:
                lines.append(key + " = " + entry[key])
        out.append("\n".join(lines) + "\n")
    sys.stdout.write("\n".join(out) + "\n")
    sys.exit(0)
if need and password != need:
    fail("ERROR: Wrong password : " + path)
wanted = [a[3:] for a in switches if a.startswith("-i!")]
if command == "t":
    if spec.get("broken"):
        fail("ERROR: Data Error : " + spec["broken"])
    sys.stdout.write("Everything is Ok\n")
    sys.exit(0)
assert command == "x" and "-so" in switches
if wanted:
    assert "-spd" in switches
for entry in spec["entries"]:
    if wanted and entry["path"] not in wanted:
        continue
    data = base64.b64decode(entry.get("stream", entry.get("data", "")))
    if wanted and "single" in entry:
        data = base64.b64decode(entry["single"])
    sys.stdout.buffer.write(data)
sys.stdout.flush()
sys.exit(spec.get("exit", 0))
'''


def fake_7z(directory) -> str:
    """Write the stand-in 7z program into ``directory`` and return its path."""
    program = Path(directory) / "fake-7z"
    program.write_text(_FAKE_7Z.replace("#!/usr/bin/python3", "#!" + sys.executable, 1), encoding="utf-8")
    program.chmod(0o755)
    return str(program)


def seven_entry(path: str, data: bytes = b"", **fields) -> dict:
    entry = {"path": path, "data": base64.b64encode(data).decode("ascii")}
    for key, value in fields.items():
        entry[key] = base64.b64encode(value).decode("ascii") if isinstance(value, bytes) else value
    return entry


def fake_seven_archive(path, entries: list[dict], **spec) -> Path:
    """A file that starts like a 7z archive and that ``fake_7z`` can list and read."""
    target = Path(path)
    target.write_bytes(SEVEN_MAGIC + json.dumps({"entries": entries, **spec}).encode("utf-8"))
    return target


class EngineCase(unittest.TestCase):
    """A TestCase with a private temporary directory (``self.tmp``) and a fixed umask."""

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="bw-archive-test-")
        self.tmp = Path(self._tmp.name)
        self._umask = os.umask(0o022)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        os.umask(self._umask)
        # Folders extracted without write permission would keep the directory from being removed.
        for directory, folders, _files in os.walk(self.tmp):
            for name in folders:
                full = os.path.join(directory, name)
                if not os.path.islink(full):
                    try:
                        os.chmod(full, 0o700)
                    except OSError:
                        pass
        self._tmp.cleanup()


def now_stamp() -> tuple:
    return time.localtime()[:6]


