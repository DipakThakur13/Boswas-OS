"""Deciding what the preview pane can show of an entry, and which entries are programs. No Qt."""

from __future__ import annotations

from .model import extension

TEXT_LIMIT = 256 * 1024                 # bytes of a text file that are read for the preview
IMAGE_LIMIT = 32 * 1024 * 1024          # an image larger than this is not decoded for the preview
IMAGE_EXTENSIONS = frozenset("png jpg jpeg gif bmp webp ico tif tiff ppm pgm pbm xpm".split())
_PROGRAM_EXTENSIONS = frozenset("desktop sh bash zsh fish run bin appimage py pl rb php js jar exe msi bat cmd com scr "
                                "ps1 vbs lnk deb rpm apk elf out".split())
_PROGRAM_MAGIC = (b"\x7fELF", b"#!", b"MZ")
_BOMS = ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))


def is_image_name(name: str) -> bool:
    return extension(name) in IMAGE_EXTENSIONS


def is_program(name: str, head: bytes = b"") -> bool:
    """Whether opening the entry could start a program: by its extension or by how its content begins.

    "Open" hands a file to the desktop's default application. Programs,
    scripts, installers and desktop entries are not handed over from inside
    an archive: the user extracts them first and then decides.
    """
    if extension(name) in _PROGRAM_EXTENSIONS:
        return True
    return any(head.startswith(magic) for magic in _PROGRAM_MAGIC)


def as_text(data: bytes) -> str | None:
    """``data`` as text when it is text (UTF-8, or UTF-16 with a byte order mark); None for binary data."""
    if not data:
        return ""
    for mark, encoding in _BOMS:
        if data.startswith(mark):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                return None
    if b"\0" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.start >= len(data) - 4:                   # the limit cut a character in two
            text = data[:error.start].decode("utf-8", "replace")
        else:
            # Not UTF-8: old 8-bit text is shown if it reads like text.
            text = data.decode("latin-1")
            if sum(1 for byte in data if byte >= 0x80) > len(data) // 3:
                return None
    controls = sum(1 for character in text if ord(character) < 32 and character not in "\n\r\t\f\x1b")
    if controls > max(4, len(text) // 50):
        return None
    return text


def clean_text(text: str) -> str:
    """Text for the preview: control characters other than line breaks and tabs become spaces."""
    return "".join(character if character in "\n\t" or ord(character) >= 32 and ord(character) != 127
                   else ("" if character == "\r" else " ") for character in text)
