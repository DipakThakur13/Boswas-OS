"""The clipboard and drag-and-drop formats file managers share (no Qt).

* ``text/uri-list``: the files, as ``file://`` URLs, one per line (RFC 2483).
* ``application/x-kde-cutselection``: ``1`` when the files were cut (Dolphin, KDE).
* ``x-special/gnome-copied-files``: ``cut`` or ``copy``, then the URLs, one
  per line (Nautilus, Nemo, Caja, Thunar, PCManFM).
* ``text/plain``: the paths, one per line, for pasting into a text.

Some versions of Nautilus put ``x-special/nautilus-clipboard`` with the
same content into the plain text; ``decode`` reads that too.
"""

from __future__ import annotations

import os
from urllib.parse import quote, unquote_to_bytes, urlsplit

URI_LIST = "text/uri-list"
KDE_CUT = "application/x-kde-cutselection"
GNOME_COPIED = "x-special/gnome-copied-files"
PLAIN = "text/plain"
_NAUTILUS_TEXT = "x-special/nautilus-clipboard"
_SAFE = "!$&'()*+,-./:=@_~"            # what GLib leaves unescaped in a file URL
_MAX_ITEMS = 100_000
_MAX_BYTES = 32 * 1024 * 1024


def path_to_uri(path: str) -> str:
    """``file:///home/me/a%20b`` for an absolute path, escaped the way GLib does (thumbnails depend on it)."""
    return "file://" + quote(os.fsencode(os.path.abspath(path)), safe=_SAFE)


def uri_to_path(uri: str) -> str | None:
    """The local path of a ``file:`` URL, or None for anything else."""
    uri = uri.strip()
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme.lower() != "file" or parts.netloc.lower() not in ("", "localhost"):
        return None
    raw = unquote_to_bytes(parts.path)
    if not raw.startswith(b"/") or b"\0" in raw:
        return None
    return os.path.normpath(os.fsdecode(raw))


def encode_uri_list(paths: list[str]) -> bytes:
    return "".join(path_to_uri(path) + "\r\n" for path in paths).encode("ascii")


def decode_uri_list(data: bytes | str) -> list[str]:
    """The local paths of a ``text/uri-list``; comments and other schemes are left out."""
    if isinstance(data, bytes):
        data = data[:_MAX_BYTES].decode("utf-8", "surrogateescape")
    paths = []
    for line in data.replace("\0", "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path = uri_to_path(line)
        if path is not None:
            paths.append(path)
            if len(paths) >= _MAX_ITEMS:
                break
    return paths


def encode_gnome(paths: list[str], cut: bool) -> bytes:
    return "\n".join(["cut" if cut else "copy", *(path_to_uri(path) for path in paths)]).encode("ascii")


def decode_gnome(data: bytes | str) -> tuple[bool, list[str]] | None:
    """(cut, paths) of ``x-special/gnome-copied-files`` content, or None when it is something else."""
    if isinstance(data, bytes):
        data = data[:_MAX_BYTES].decode("utf-8", "surrogateescape")
    lines = [line.strip() for line in data.replace("\0", "").splitlines() if line.strip()]
    if lines and lines[0] == _NAUTILUS_TEXT:
        lines = lines[1:]
    if not lines or lines[0] not in ("cut", "copy"):
        return None
    return lines[0] == "cut", decode_uri_list("\n".join(lines[1:]))


def encode(paths: list[str], cut: bool = False) -> dict[str, bytes]:
    """Every format for ``paths``, by MIME type."""
    return {
        URI_LIST: encode_uri_list(paths),
        GNOME_COPIED: encode_gnome(paths, cut),
        KDE_CUT: b"1" if cut else b"0",
        PLAIN: "\n".join(paths).encode("utf-8", "surrogateescape"),
    }


def decode(formats: dict[str, bytes]) -> tuple[list[str], bool]:
    """(paths, cut) from the formats another application offered. Plain text is not taken for files."""
    cut = formats.get(KDE_CUT, b"").strip().startswith(b"1")
    gnome = decode_gnome(formats[GNOME_COPIED]) if GNOME_COPIED in formats else None
    plain = formats.get(PLAIN)
    if gnome is None and plain is not None and plain.lstrip().startswith(_NAUTILUS_TEXT.encode("ascii")):
        gnome = decode_gnome(plain)
    paths = decode_uri_list(formats[URI_LIST]) if URI_LIST in formats else []
    if gnome is not None:
        cut = cut or gnome[0]
        paths = paths or gnome[1]
    return paths, cut


def copy_as_path(paths: list[str]) -> str:
    """The text "Copy as path" puts on the clipboard: one path per line."""
    return "\n".join(paths)
