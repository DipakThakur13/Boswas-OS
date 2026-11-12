"""A minimal, bounded BER/DER reader: enough to take the certificates out of a PKCS#7 SignedData.

JAR (APK v1) signature blocks (META-INF/*.RSA, *.DSA, *.EC) are CMS/PKCS#7
ContentInfo structures; some signing tools emit indefinite lengths (BER), so
both definite and indefinite lengths are accepted. Certificates themselves
are DER and are returned byte-exact (for their SHA-256). Nothing is
interpreted beyond tags and lengths; signatures are not verified here
(Android verifies them when it installs the APK).
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_DEPTH = 32
MAX_ITEMS = 10_000


class DerError(ValueError):
    pass


@dataclass(frozen=True)
class Tlv:
    tag: int            # first identifier octet (class, constructed bit, low tag number)
    start: int          # offset of the identifier octet
    content: int        # offset of the content
    end: int            # offset just past the element (after end-of-contents for indefinite lengths)
    content_end: int    # offset just past the content
    constructed: bool


def read_tlv(data: bytes, pos: int, limit: int, depth: int = 0) -> Tlv:
    if depth > MAX_DEPTH:
        raise DerError("structure nested too deeply")
    if pos + 2 > limit:
        raise DerError("truncated element")
    start = pos
    tag = data[pos]
    pos += 1
    if tag & 0x1F == 0x1F:                       # high tag number form
        for _ in range(5):
            if pos >= limit:
                raise DerError("truncated tag")
            more = data[pos] & 0x80
            pos += 1
            if not more:
                break
        else:
            raise DerError("tag number too large")
    if pos >= limit:
        raise DerError("truncated length")
    first = data[pos]
    pos += 1
    constructed = bool(tag & 0x20)
    if first == 0x80:                            # indefinite length (BER, constructed only)
        if not constructed:
            raise DerError("indefinite length on a primitive element")
        cursor = pos
        for _ in range(MAX_ITEMS):
            if cursor + 2 <= limit and data[cursor] == 0 and data[cursor + 1] == 0:
                return Tlv(tag, start, pos, cursor + 2, cursor, True)
            child = read_tlv(data, cursor, limit, depth + 1)
            cursor = child.end
        raise DerError("too many elements")
    if first & 0x80:
        count = first & 0x7F
        if count == 0 or count > 4 or pos + count > limit:
            raise DerError("invalid length")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    else:
        length = first
    end = pos + length
    if end > limit:
        raise DerError("element extends past its container")
    return Tlv(tag, start, pos, end, end, constructed)


def children(data: bytes, parent: Tlv, depth: int = 0) -> list[Tlv]:
    if not parent.constructed:
        raise DerError("not a constructed element")
    items, pos = [], parent.content
    while pos < parent.content_end:
        if len(items) >= MAX_ITEMS:
            raise DerError("too many elements")
        child = read_tlv(data, pos, parent.content_end, depth + 1)
        items.append(child)
        pos = child.end
    return items


SEQUENCE, SET, OID = 0x30, 0x31, 0x06
CONTEXT_0_CONSTRUCTED = 0xA0
SIGNED_DATA_OID = bytes.fromhex("2a864886f70d010702")      # 1.2.840.113549.1.7.2


def pkcs7_certificates(data: bytes) -> list[bytes]:
    """DER encodings of the certificates in a PKCS#7 / CMS SignedData ContentInfo."""
    top = read_tlv(data, 0, len(data))
    if top.tag != SEQUENCE:
        raise DerError("not a PKCS#7 ContentInfo")
    parts = children(data, top)
    if len(parts) < 2 or parts[0].tag != OID or data[parts[0].content:parts[0].content_end] != SIGNED_DATA_OID:
        raise DerError("not a PKCS#7 SignedData")
    explicit = parts[1]
    if explicit.tag != CONTEXT_0_CONSTRUCTED:
        raise DerError("SignedData content missing")
    inner = children(data, explicit)
    if not inner or inner[0].tag != SEQUENCE:
        raise DerError("SignedData is not a SEQUENCE")
    certificates = []
    for field in children(data, inner[0]):
        if field.tag == CONTEXT_0_CONSTRUCTED:          # certificates [0] IMPLICIT SET OF Certificate
            for cert in children(data, field):
                if cert.tag == SEQUENCE:
                    certificates.append(bytes(data[cert.start:cert.end]))
            break
    return certificates
