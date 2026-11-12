"""What kind of installable file the user chose, from its content (never its name).

  Debian package   ar archive ("!<arch>\\n") whose first member is debian-binary
  Windows program  PE ("MZ") or Windows Installer package (OLE compound file)
  AppImage         ELF with the AppImage type-2 magic "AI\\x02" at offset 8
  Android app      ZIP archive containing AndroidManifest.xml

Only the first bytes are read, plus the ZIP directory for archives. Nothing
in the file is executed or unpacked.
"""

from __future__ import annotations

import os
import stat
import zipfile
from dataclasses import dataclass

DEB, WINDOWS, MSI, APPIMAGE, APPIMAGE_TYPE1, APK, ZIP, UNKNOWN = (
    "deb", "windows", "msi", "appimage", "appimage-type1", "apk", "zip", "unknown")
OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
HEAD = 4096
MAX_FILE = 64 * 1024 ** 3

DESCRIPTIONS = {
    DEB: "Debian package",
    WINDOWS: "Windows program",
    MSI: "Windows Installer package",
    APPIMAGE: "AppImage application",
    APPIMAGE_TYPE1: "AppImage (old type 1 format)",
    APK: "Android app (APK)",
    ZIP: "ZIP archive",
    UNKNOWN: "Unknown file type",
}


@dataclass(frozen=True)
class FileKind:
    kind: str
    path: str
    size: int
    detail: str = ""

    @property
    def description(self) -> str:
        return DESCRIPTIONS.get(self.kind, DESCRIPTIONS[UNKNOWN])


def classify(head: bytes, zip_names: list[str] | None = None) -> str:
    """The kind of a file from its first bytes (and, for ZIP archives, its member names)."""
    if head.startswith(b"!<arch>\n") and head[8:24].rstrip(b" /") == b"debian-binary":
        return DEB
    if head.startswith(b"\x7fELF"):
        if head[8:11] == b"AI\x02":
            return APPIMAGE
        if head[8:11] == b"AI\x01":
            return APPIMAGE_TYPE1
        return UNKNOWN
    if head.startswith(b"MZ"):
        return WINDOWS
    if head.startswith(OLE_MAGIC):
        return MSI
    if head.startswith(b"PK\x03\x04"):
        return APK if zip_names is not None and "AndroidManifest.xml" in zip_names else ZIP
    return UNKNOWN


def detect(path: str) -> FileKind:
    """Classify a regular file chosen by the user. Raises ValueError with a message for the user."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path or "\n" in path:
        raise ValueError("Choose a file by its full path.")
    try:
        info = os.stat(path)
    except OSError as exc:
        raise ValueError(f"The file cannot be read ({exc.strerror or exc}).") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("This is not a regular file.")
    if info.st_size > MAX_FILE:
        raise ValueError("The file is too large to be an installable application.")
    try:
        with open(path, "rb") as handle:
            head = handle.read(HEAD)
    except OSError as exc:
        raise ValueError(f"The file cannot be read ({exc.strerror or exc}).") from None
    names = None
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()[:100000]
        except (OSError, zipfile.BadZipFile, ValueError):
            names = []
    return FileKind(classify(head, names), path, info.st_size)
