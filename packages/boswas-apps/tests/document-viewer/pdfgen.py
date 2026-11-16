"""PDF files written by hand for the Document Viewer tests.

``make_pdf`` writes a small, valid PDF with text pages in Helvetica (a font
every PDF reader has, so nothing is embedded), and optionally an outline,
link annotations, document information and the standard RC4 encryption
with a user password. Everything is plain Python.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[3] / "desktop" / "ui" / "qt"))

try:
    import PySide6  # noqa: F401
    PYSIDE = True
except ImportError:
    PYSIDE = False

_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def _rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) & 0xFF])
    return bytes(out)


def _literal(data: bytes) -> bytes:
    return b"(" + data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").replace(b"\r", b"\\r") + b")"


class _Writer:
    def __init__(self, password: str | None) -> None:
        self.objects: dict[int, tuple] = {}
        self.count = 0
        self.password = password
        self.key: bytes | None = None
        self.file_id = hashlib.md5(b"boswas test document").digest()
        self.encrypt_object = 0
        if password is not None:
            user = (password.encode("latin-1") + _PAD)[:32]
            owner_key = hashlib.md5(user).digest()[:5]
            self.owner = _rc4(owner_key, user)
            self.permissions = -4
            self.key = hashlib.md5(user + self.owner + (self.permissions & 0xFFFFFFFF).to_bytes(4, "little")
                                   + self.file_id).digest()[:5]
            self.user = _rc4(self.key, _PAD)

    def reserve(self) -> int:
        self.count += 1
        return self.count

    def _object_key(self, number: int) -> bytes:
        return hashlib.md5(self.key + number.to_bytes(3, "little") + b"\0\0").digest()[:10]

    def string(self, number: int, text: str) -> bytes:
        try:
            data = text.encode("latin-1")
        except UnicodeEncodeError:
            data = b"\xfe\xff" + text.encode("utf-16-be")
        if self.key is not None and number != self.encrypt_object:
            data = _rc4(self._object_key(number), data)
        return _literal(data)

    def set(self, number: int, body: bytes, stream: bytes | None = None) -> None:
        if stream is not None and self.key is not None:
            stream = _rc4(self._object_key(number), stream)
        self.objects[number] = (body, stream)

    def write(self, path, root: int, info: int | None) -> None:
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = {}
        for number in sorted(self.objects):
            body, stream = self.objects[number]
            offsets[number] = len(out)
            out += f"{number} 0 obj\n".encode()
            if stream is None:
                out += body + b"\nendobj\n"
            else:
                out += body[:-2] + f" /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream\nendobj\n"
        start = len(out)
        out += f"xref\n0 {self.count + 1}\n".encode() + b"0000000000 65535 f \n"
        for number in range(1, self.count + 1):
            out += f"{offsets[number]:010d} 00000 n \n".encode()
        trailer = f"<< /Size {self.count + 1} /Root {root} 0 R"
        if info is not None:
            trailer += f" /Info {info} 0 R"
        trailer += f" /ID [<{self.file_id.hex()}> <{self.file_id.hex()}>]"
        if self.key is not None:
            trailer += f" /Encrypt {self.encrypt_object} 0 R"
        out += f"trailer\n{trailer} >>\nstartxref\n{start}\n%%EOF\n".encode()
        Path(path).write_bytes(bytes(out))


def page_lines(index: int, lines: int = 30) -> list[str]:
    """The default text of page ``index`` (zero-based): numbered lines that can be searched for."""
    return [f"Page {index + 1} line {line + 1}: the quick brown fox jumps over the lazy dog." for line in range(lines)]


def make_pdf(path, pages: int | list = 3, *, size: tuple = (595, 842), sizes: list | None = None,
             outline: list | None = None, links: list | None = None, info: dict | None = None,
             password: str | None = None, lines: int = 30) -> Path:
    """Write a PDF.

    ``pages``: a number (default text) or a list with the lines of each page.
    ``sizes``: a (width, height) in points for each page. ``outline``: a list
    of ``(title, page_index, children)``. ``links``: a list of
    ``(page_index, (x, y_from_top, width, height), target)`` where the target
    is a page index or a URL. ``info``: Title, Author, Subject, Keywords,
    Creator, Producer. ``password``: the user password of an encrypted file.
    """
    texts = [page_lines(index, lines) for index in range(pages)] if isinstance(pages, int) else list(pages)
    count = len(texts)
    writer = _Writer(password)
    catalog, tree, font = writer.reserve(), writer.reserve(), writer.reserve()
    page_numbers = [writer.reserve() for _ in range(count)]
    content_numbers = [writer.reserve() for _ in range(count)]
    dimensions = [tuple(sizes[index]) if sizes else tuple(size) for index in range(count)]
    annotations: dict[int, list[int]] = {}
    for page, box, target in links or []:
        number = writer.reserve()
        width, height = dimensions[page]
        x, top, w, h = box
        rect = f"[{x} {height - top - h} {x + w} {height - top}]"
        if isinstance(target, int):
            action = f"/Dest [{page_numbers[target]} 0 R /XYZ 0 {dimensions[target][1]} 0]"
        else:
            action = "/A << /S /URI /URI " + writer.string(number, target).decode("latin-1") + " >>"
        writer.set(number, f"<< /Type /Annot /Subtype /Link /Rect {rect} /Border [0 0 0] {action} >>".encode("latin-1"))
        annotations.setdefault(page, []).append(number)
    for index in range(count):
        width, height = dimensions[index]
        annots = ""
        if index in annotations:
            annots = " /Annots [" + " ".join(f"{number} 0 R" for number in annotations[index]) + "]"
        writer.set(page_numbers[index], (f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 {width} {height}] "
                                         f"/Contents {content_numbers[index]} 0 R /Resources << /Font << /F1 {font} 0 R "
                                         f">> >>{annots} >>").encode())
        stream = bytearray(f"BT /F1 12 Tf 72 {height - 72} Td 16 TL\n".encode())
        for line in texts[index]:
            stream += _literal(line.encode("latin-1", "replace")) + b" Tj T*\n"
        stream += b"ET"
        writer.set(content_numbers[index], b"<< >>", bytes(stream))
    writer.set(font, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    writer.set(tree, f"<< /Type /Pages /Kids [{' '.join(f'{n} 0 R' for n in page_numbers)}] /Count {count} >>".encode())
    outline_root = None
    if outline:
        outline_root = writer.reserve()

        def build(items: list, parent: int) -> tuple[int, int, int]:
            numbers = [writer.reserve() for _ in items]
            total = 0
            for position, (title, page, children) in enumerate(items):
                number = numbers[position]
                body = f"<< /Title {writer.string(number, title).decode('latin-1')} /Parent {parent} 0 R"
                if position > 0:
                    body += f" /Prev {numbers[position - 1]} 0 R"
                if position + 1 < len(items):
                    body += f" /Next {numbers[position + 1]} 0 R"
                body += f" /Dest [{page_numbers[page]} 0 R /XYZ 0 {dimensions[page][1]} 0]"
                total += 1
                if children:
                    first, last, inner = build(children, number)
                    body += f" /First {first} 0 R /Last {last} 0 R /Count {inner}"
                    total += inner
                writer.set(number, (body + " >>").encode("latin-1"))
            return numbers[0], numbers[-1], total

        first, last, total = build(outline, outline_root)
        writer.set(outline_root, f"<< /Type /Outlines /First {first} 0 R /Last {last} 0 R /Count {total} >>".encode())
    catalog_body = f"<< /Type /Catalog /Pages {tree} 0 R"
    if outline_root:
        catalog_body += f" /Outlines {outline_root} 0 R /PageMode /UseOutlines"
    writer.set(catalog, (catalog_body + " >>").encode())
    info_number = None
    if info:
        info_number = writer.reserve()
        fields = " ".join(f"/{key} {writer.string(info_number, value).decode('latin-1')}" for key, value in info.items())
        writer.set(info_number, f"<< {fields} >>".encode("latin-1"))
    if password is not None:
        writer.encrypt_object = writer.reserve()
        writer.set(writer.encrypt_object, (f"<< /Filter /Standard /V 1 /R 2 /O <{writer.owner.hex()}> "
                                           f"/U <{writer.user.hex()}> /P {writer.permissions} >>").encode())
    writer.write(path, catalog, info_number)
    return Path(path)


def temp_home(case) -> None:
    """Nothing: FluentTestCase already gives every test private XDG directories."""


__all__ = ["make_pdf", "page_lines", "PYSIDE", "HERE"]
_ = os
