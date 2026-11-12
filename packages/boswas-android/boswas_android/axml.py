"""Reader for Android's binary XML (AXML), as used by AndroidManifest.xml in an APK.

Only what manifest inspection needs: the string pool (UTF-8 and UTF-16),
the resource map (attribute names -> android:attr resource IDs) and the
start/end element chunks with typed attribute values. Every offset and
count is checked against the chunk it belongs to; malformed input raises
AxmlError, never an IndexError or struct.error.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

RES_STRING_POOL_TYPE = 0x0001
RES_XML_TYPE = 0x0003
RES_XML_START_NAMESPACE_TYPE = 0x0100
RES_XML_END_NAMESPACE_TYPE = 0x0101
RES_XML_START_ELEMENT_TYPE = 0x0102
RES_XML_END_ELEMENT_TYPE = 0x0103
RES_XML_CDATA_TYPE = 0x0104
RES_XML_RESOURCE_MAP_TYPE = 0x0180

UTF8_FLAG = 1 << 8
NO_INDEX = 0xFFFFFFFF

TYPE_NULL = 0x00
TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
TYPE_INT_BOOLEAN = 0x12

MAX_STRINGS = 200_000
MAX_ELEMENTS = 200_000
MAX_ATTRIBUTES = 1_000
MAX_DEPTH = 64

ANDROID_NS = "http://schemas.android.com/apk/res/android"


class AxmlError(ValueError):
    pass


@dataclass
class Attribute:
    namespace: str | None
    name: str
    resource_id: int | None
    type: int
    data: int
    raw: str | None          # raw string value, if any

    @property
    def value(self):
        """Python value: str for strings, int for integers, bool for booleans, None for references."""
        if self.type == TYPE_STRING:
            return self.raw
        if self.type in (TYPE_INT_DEC, TYPE_INT_HEX):
            return self.data - (1 << 32) if self.data & 0x80000000 else self.data
        if self.type == TYPE_INT_BOOLEAN:
            return self.data != 0
        if self.type == TYPE_REFERENCE:
            return None
        return self.raw


@dataclass
class Element:
    name: str
    depth: int
    path: tuple[str, ...]
    attributes: list[Attribute] = field(default_factory=list)

    def attr(self, name: str, resource_id: int | None = None, namespace: str | None = ANDROID_NS) -> Attribute | None:
        """Attribute by resource ID (authoritative, survives obfuscated names) or by namespace and name."""
        if resource_id is not None:
            for attribute in self.attributes:
                if attribute.resource_id == resource_id:
                    return attribute
        for attribute in self.attributes:
            if attribute.name == name and (attribute.namespace or None) == namespace:
                return attribute
        return None


def _u16(data: bytes, pos: int) -> int:
    return struct.unpack_from("<H", data, pos)[0]


def _u32(data: bytes, pos: int) -> int:
    return struct.unpack_from("<I", data, pos)[0]


def _string_pool(data: bytes, start: int, header_size: int, size: int) -> list[str]:
    end = start + size
    if header_size < 28 or start + 28 > end:
        raise AxmlError("string pool header truncated")
    count, _styles, flags, strings_start, _styles_start = struct.unpack_from("<IIIII", data, start + 8)
    if count > MAX_STRINGS:
        raise AxmlError("string pool too large")
    offsets_at = start + header_size
    if offsets_at + 4 * count > end:
        raise AxmlError("string pool offsets truncated")
    base = start + strings_start
    if strings_start and base > end:
        raise AxmlError("string data outside the pool")
    utf8 = bool(flags & UTF8_FLAG)
    strings = []
    for i in range(count):
        pos = base + _u32(data, offsets_at + 4 * i)
        if pos >= end:
            raise AxmlError("string offset outside the pool")
        if utf8:
            # UTF-16 length (1 or 2 bytes), then UTF-8 byte length (1 or 2 bytes), bytes, NUL
            n = data[pos]
            pos += 2 if n & 0x80 else 1
            if pos >= end:
                raise AxmlError("string truncated")
            length = data[pos]
            if length & 0x80:
                if pos + 1 >= end:
                    raise AxmlError("string truncated")
                length = ((length & 0x7F) << 8) | data[pos + 1]
                pos += 2
            else:
                pos += 1
            if pos + length > end:
                raise AxmlError("string truncated")
            strings.append(data[pos:pos + length].decode("utf-8", errors="replace"))
        else:
            if pos + 2 > end:
                raise AxmlError("string truncated")
            length = _u16(data, pos)
            pos += 2
            if length & 0x8000:
                if pos + 2 > end:
                    raise AxmlError("string truncated")
                length = ((length & 0x7FFF) << 16) | _u16(data, pos)
                pos += 2
            if pos + 2 * length > end:
                raise AxmlError("string truncated")
            strings.append(data[pos:pos + 2 * length].decode("utf-16-le", errors="replace"))
    return strings


def parse(data: bytes) -> list[Element]:
    """All start elements of the document, in order, with their attributes."""
    try:
        return _parse(data)
    except AxmlError:
        raise
    except (struct.error, IndexError, OverflowError) as exc:
        raise AxmlError(f"malformed binary XML ({exc})") from None


def _parse(data: bytes) -> list[Element]:
    if len(data) < 8:
        raise AxmlError("file too short")
    doc_type, doc_header, doc_size = struct.unpack_from("<HHI", data, 0)
    if doc_type != RES_XML_TYPE or doc_header < 8:
        raise AxmlError("not a binary XML document")
    limit = min(len(data), doc_size) if doc_size >= 8 else len(data)
    pos = doc_header
    strings: list[str] = []
    resource_ids: list[int] = []
    elements: list[Element] = []
    stack: list[str] = []

    def string(index: int) -> str | None:
        if index == NO_INDEX:
            return None
        if index >= len(strings):
            raise AxmlError("string index out of range")
        return strings[index]

    while pos + 8 <= limit:
        chunk_type, header_size, size = struct.unpack_from("<HHI", data, pos)
        if size < 8 or header_size < 8 or header_size > size or pos + size > limit:
            raise AxmlError(f"invalid chunk at offset {pos}")
        if chunk_type == RES_STRING_POOL_TYPE:
            strings = _string_pool(data, pos, header_size, size)
        elif chunk_type == RES_XML_RESOURCE_MAP_TYPE:
            n = (size - header_size) // 4
            resource_ids = [_u32(data, pos + header_size + 4 * i) for i in range(n)]
        elif chunk_type == RES_XML_START_ELEMENT_TYPE:
            if len(elements) >= MAX_ELEMENTS or len(stack) >= MAX_DEPTH:
                raise AxmlError("document too large or nested too deeply")
            ext = pos + header_size
            if ext + 20 > pos + size:
                raise AxmlError("element truncated")
            ns_index, name_index = struct.unpack_from("<II", data, ext)
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, ext + 8)
            if attr_count > MAX_ATTRIBUTES or (attr_count and attr_size < 20):
                raise AxmlError("invalid attribute table")
            name = string(name_index) or ""
            element = Element(name, len(stack), tuple(stack) + (name,))
            for i in range(attr_count):
                at = ext + attr_start + i * attr_size
                if at + 20 > pos + size:
                    raise AxmlError("attribute outside its element")
                a_ns, a_name, a_raw = struct.unpack_from("<III", data, at)
                _vsize, _res0, a_type = struct.unpack_from("<HBB", data, at + 12)
                a_data = _u32(data, at + 16)
                attr_name = string(a_name) or ""
                rid = resource_ids[a_name] if a_name != NO_INDEX and a_name < len(resource_ids) else None
                raw = string(a_raw) if a_raw != NO_INDEX else None
                if a_type == TYPE_STRING and raw is None:
                    raw = string(a_data)
                element.attributes.append(Attribute(string(a_ns), attr_name, rid, a_type, a_data, raw))
            elements.append(element)
            stack.append(name)
        elif chunk_type == RES_XML_END_ELEMENT_TYPE:
            if not stack:
                raise AxmlError("unbalanced end element")
            stack.pop()
        elif chunk_type in (RES_XML_START_NAMESPACE_TYPE, RES_XML_END_NAMESPACE_TYPE, RES_XML_CDATA_TYPE):
            pass
        # Unknown chunk types are skipped by size (forward compatible).
        pos += size
    if not elements:
        raise AxmlError("no elements in the document")
    return elements
