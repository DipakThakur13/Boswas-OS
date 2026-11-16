"""Which file is which kind of archive, and what this system can do with each kind. No Qt.

A file is recognised by its first bytes, never by its name alone: a name
only decides between kinds that look the same from the outside (a ``.jar``
is a ZIP file). A compressed stream is opened far enough to see whether a
TAR archive is inside.

What can be read depends on the machine: Zstandard needs a Python module
(``compression.zstd`` of Python 3.14 or ``zstandard``), 7z, RAR and
AES-encrypted ZIP entries need the ``7z`` program. ``support()`` reports
what is there, and the window shows it; nothing is offered that would fail.
"""

from __future__ import annotations

import bz2
import gzip
import importlib
import lzma
import os
import tarfile
import zipfile
from dataclasses import dataclass

from . import sevenzip
from .errors import ArchiveError, UnsupportedFormat


@dataclass(frozen=True)
class Format:
    key: str                       # zip, tar, tar.gz, tar.bz2, tar.xz, tar.zst, gz, bz2, xz, zst, 7z, rar
    title: str
    extension: str                 # the extension a new archive of this kind gets
    family: str                    # zip, tar, stream, 7z
    compression: str = ""          # for tar and stream: "", gz, bz2, xz, zst
    writable: bool = False         # this application can create and change it


FORMATS = {
    "zip": Format("zip", "ZIP archive", ".zip", "zip", writable=True),
    "tar": Format("tar", "TAR archive", ".tar", "tar", writable=True),
    "tar.gz": Format("tar.gz", "TAR archive, gzip-compressed", ".tar.gz", "tar", "gz", writable=True),
    "tar.bz2": Format("tar.bz2", "TAR archive, bzip2-compressed", ".tar.bz2", "tar", "bz2", writable=True),
    "tar.xz": Format("tar.xz", "TAR archive, xz-compressed", ".tar.xz", "tar", "xz", writable=True),
    "tar.zst": Format("tar.zst", "TAR archive, Zstandard-compressed", ".tar.zst", "tar", "zst"),
    "gz": Format("gz", "Gzip-compressed file", ".gz", "stream", "gz"),
    "bz2": Format("bz2", "Bzip2-compressed file", ".bz2", "stream", "bz2"),
    "xz": Format("xz", "XZ-compressed file", ".xz", "stream", "xz"),
    "zst": Format("zst", "Zstandard-compressed file", ".zst", "stream", "zst"),
    "7z": Format("7z", "7z archive", ".7z", "7z"),
    "rar": Format("rar", "RAR archive", ".rar", "7z"),
}
CREATABLE = ("zip", "tar", "tar.gz", "tar.xz", "tar.bz2")

# Extensions that are removed to name the folder an archive is extracted into, longest first.
_STRIP = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.zstd", ".tgz", ".tbz2", ".tbz", ".txz", ".tzst",
          ".zip", ".tar", ".gz", ".bz2", ".xz", ".zst", ".zstd", ".7z", ".rar", ".jar", ".apk", ".epub")
_ZIP_LIKE = (".zip", ".jar", ".apk", ".epub", ".war", ".ear", ".xpi", ".odt", ".ods", ".odp", ".docx", ".xlsx",
             ".pptx", ".whl", ".cbz")
_MAGIC = (
    (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"), (b"PK\x07\x08", "zip"),
    (b"\x1f\x8b", "gz"), (b"BZh", "bz2"), (b"\xfd7zXZ\x00", "xz"), (b"\x28\xb5\x2f\xfd", "zst"),
    (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!\x1a\x07", "rar"),
)
_PEEK = 512


def zstd_module():
    """The Zstandard module of this Python (``compression.zstd`` or ``zstandard``), or None."""
    for name in ("compression.zstd", "zstandard"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def open_zstd(path: str):
    """A readable binary file of the decompressed data of a Zstandard file; UnsupportedFormat without a module."""
    module = zstd_module()
    if module is None:
        raise UnsupportedFormat("Zstandard archives cannot be opened on this system: Python has no Zstandard "
                                "module here.", needs="python3-zstandard")
    if hasattr(module, "ZstdFile"):                       # compression.zstd (Python 3.14)
        return module.ZstdFile(path, "rb")
    raw = open(path, "rb")                                # the zstandard package
    try:
        return module.ZstdDecompressor().stream_reader(raw, closefd=True)
    except Exception:
        raw.close()
        raise


def open_stream(path: str, compression: str):
    """A readable binary file of the decompressed data of a gz, bz2, xz or zst file."""
    if compression == "gz":
        return gzip.open(path, "rb")
    if compression == "bz2":
        return bz2.open(path, "rb")
    if compression == "xz":
        return lzma.open(path, "rb")
    if compression == "zst":
        return open_zstd(path)
    raise ValueError(f"unknown compression {compression!r}")


_TAR_NAMES = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz", ".tar.zst", ".tar.zstd", ".tzst")


def _all_zero(block: bytes) -> bool:
    return len(block) >= 512 and not any(block)


def _looks_like_tar(block: bytes) -> bool:
    if len(block) < 512:
        return False
    if block[257:262] == b"ustar":
        return True
    try:
        tarfile.TarInfo.frombuf(block[:512], "utf-8", "surrogateescape")
    except Exception:              # tarfile raises its own HeaderError family and ValueError
        return False
    return True


def detect(path: str | os.PathLike) -> Format:
    """The Format of the file at ``path``.

    Raises ``ArchiveError`` for a file that cannot be read and
    ``UnsupportedFormat`` for one that is no archive this application knows.
    """
    path = os.fspath(path)
    try:
        if os.path.isdir(path):
            raise UnsupportedFormat("That is a folder, not an archive.")
        with open(path, "rb") as handle:
            head = handle.read(_PEEK)
    except OSError as error:
        raise ArchiveError(f"The file cannot be read: {error.strerror or error}") from None
    lowered = os.path.basename(path).lower()
    kind = next((key for magic, key in _MAGIC if head.startswith(magic)), None)
    if kind is None and (_looks_like_tar(head) or (_all_zero(head) and lowered.endswith(".tar"))):
        kind = "tar"                 # an archive without entries is nothing but zero blocks
    if kind is None:
        # A ZIP file may start with other data (a self-extracting program): its directory is at the end.
        if lowered.endswith(_ZIP_LIKE + (".exe",)) and zipfile.is_zipfile(path):
            kind = "zip"
    if kind is None:
        if not head:
            raise UnsupportedFormat("The file is empty.")
        raise UnsupportedFormat("This file is not an archive that Archive Manager can open.")
    if kind in ("gz", "bz2", "xz", "zst"):
        try:
            with open_stream(path, kind) as stream:
                inner = stream.read(_PEEK)
        except UnsupportedFormat:
            if lowered.endswith((".tar.zst", ".tar.zstd", ".tzst")):
                return FORMATS["tar.zst"]
            return FORMATS["zst"]
        except (OSError, EOFError, ValueError, lzma.LZMAError) as error:
            raise ArchiveError(f"The compressed data is damaged: {error}") from None
        except Exception as error:                         # a zstandard module raises its own error type
            raise ArchiveError(f"The compressed data is damaged: {error}") from None
        if _looks_like_tar(inner) or (_all_zero(inner) and lowered.endswith(_TAR_NAMES)):
            return FORMATS[f"tar.{kind}"]
    return FORMATS[kind]


def strip_extension(name: str) -> str:
    """``photos.tar.gz`` → ``photos``: the name of the folder an archive is extracted into, or of a stream's file."""
    lowered = name.lower()
    for extension in _STRIP:
        if lowered.endswith(extension) and len(name) > len(extension):
            return name[:-len(extension)]
    return name + ".extracted" if "." not in name.strip(".") else name.rsplit(".", 1)[0]


def stream_inner_name(name: str) -> str:
    """The name of the file inside ``report.txt.gz`` (``report.txt``); ``.tgz`` and friends become ``.tar``."""
    lowered = name.lower()
    for short, long_ in ((".tgz", ".tar"), (".tbz2", ".tar"), (".tbz", ".tar"), (".txz", ".tar"), (".tzst", ".tar")):
        if lowered.endswith(short) and len(name) > len(short):
            return name[:-len(short)] + long_
    for extension in (".gz", ".bz2", ".xz", ".zst", ".zstd"):
        if lowered.endswith(extension) and len(name) > len(extension):
            return name[:-len(extension)]
    return name + ".out"


def format_for_new(path: str) -> Format | None:
    """The Format a new archive named ``path`` gets from its extension; None when the extension names none."""
    lowered = os.path.basename(path).lower()
    table = ((".tar.gz", "tar.gz"), (".tgz", "tar.gz"), (".tar.xz", "tar.xz"), (".txz", "tar.xz"),
             (".tar.bz2", "tar.bz2"), (".tbz2", "tar.bz2"), (".tbz", "tar.bz2"), (".tar", "tar"), (".zip", "zip"))
    for extension, key in table:
        if lowered.endswith(extension):
            return FORMATS[key]
    return None


@dataclass(frozen=True)
class Support:
    title: str
    state: str                     # "read and write", "read", "not available"
    note: str = ""


def support() -> list[Support]:
    """What this system can do with each kind of archive, for the window to show."""
    seven = sevenzip.find_program()
    zstd = zstd_module() is not None
    needs_7z = "needs the 7zip package"
    return [
        Support("ZIP (also JAR, APK, EPUB)", "read and write",
                "AES-encrypted entries are read with 7z" if seven else f"AES-encrypted entries: {needs_7z}"),
        Support("TAR, TAR.GZ, TAR.BZ2, TAR.XZ", "read and write"),
        Support("GZ, BZ2, XZ (one compressed file)", "read"),
        Support("ZST, TAR.ZST", "read" if zstd else "not available",
                "" if zstd else "this Python has no Zstandard module"),
        Support("7z", "read" if seven else "not available", "" if seven else needs_7z),
        Support("RAR", "read" if seven else "not available",
                "needs the RAR codec of 7z (7zip-rar)" if seven else needs_7z),
    ]


OPEN_FILTER = ("Archives (*.zip *.jar *.apk *.epub *.tar *.tar.gz *.tgz *.tar.bz2 *.tbz2 *.tar.xz *.txz *.tar.zst "
               "*.gz *.bz2 *.xz *.zst *.7z *.rar);;All files (*)")
