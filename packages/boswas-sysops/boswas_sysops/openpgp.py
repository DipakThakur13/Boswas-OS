"""OpenPGP public key fingerprints (RFC 4880 / RFC 9580), without gpg.

Used to check that the Waydroid repository keyring shipped by boswas-android
contains exactly the key whose fingerprint runtime.conf pins, before APT is
told to trust it. Only packet framing and the public-key packet body are
parsed; nothing is decrypted or verified here (APT verifies signatures).
"""

from __future__ import annotations

import base64
import binascii
import hashlib

PUBLIC_KEY, PUBLIC_SUBKEY = 6, 14
ARMOR_BEGIN = b"-----BEGIN PGP PUBLIC KEY BLOCK-----"
ARMOR_END = b"-----END PGP PUBLIC KEY BLOCK-----"
CR, LF = b"\r", b"\n"


class KeyringError(ValueError):
    pass


def dearmor(data: bytes) -> bytes:
    """Binary packets of an ASCII-armored key block (binary input is returned unchanged).

    Armor header lines contain ": ", the checksum line starts with "=";
    neither can occur in a base64 data line.
    """
    if ARMOR_BEGIN not in data:
        return data
    body = data.split(ARMOR_BEGIN, 1)[1].split(ARMOR_END, 1)[0]
    lines = [line.strip() for line in body.replace(CR, b"").split(LF)]
    payload = b"".join(line for line in lines if line and b":" not in line and not line.startswith(b"="))
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyringError(f"invalid ASCII armor: {exc}") from None


def packets(data: bytes):
    """(tag, body) of every packet; raises KeyringError on malformed framing."""
    pos = 0
    while pos < len(data):
        header = data[pos]
        if not header & 0x80:
            raise KeyringError(f"not an OpenPGP packet at offset {pos}")
        pos += 1
        if header & 0x40:                                  # new format
            tag = header & 0x3F
            if pos >= len(data):
                raise KeyringError("truncated packet header")
            first = data[pos]
            if first < 192:
                length, pos = first, pos + 1
            elif first < 224:
                if pos + 1 >= len(data):
                    raise KeyringError("truncated packet length")
                length, pos = ((first - 192) << 8) + data[pos + 1] + 192, pos + 2
            elif first == 255:
                if pos + 5 > len(data):
                    raise KeyringError("truncated packet length")
                length, pos = int.from_bytes(data[pos + 1:pos + 5], "big"), pos + 5
            else:
                raise KeyringError("partial body lengths are not valid in a keyring")
        else:                                              # old format
            tag = (header >> 2) & 0x0F
            size = {0: 1, 1: 2, 2: 4}.get(header & 0x03)
            if size is None:
                raise KeyringError("indeterminate packet length")
            if pos + size > len(data):
                raise KeyringError("truncated packet length")
            length, pos = int.from_bytes(data[pos:pos + size], "big"), pos + size
        if pos + length > len(data):
            raise KeyringError("truncated packet body")
        yield tag, data[pos:pos + length]
        pos += length


def fingerprint(body: bytes) -> str:
    if not body:
        raise KeyringError("empty key packet")
    version = body[0]
    if version == 4:
        digest = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body, usedforsecurity=False).hexdigest()
    elif version == 5:
        digest = hashlib.sha256(b"\x9a" + len(body).to_bytes(4, "big") + body).hexdigest()
    elif version == 6:
        digest = hashlib.sha256(b"\x9b" + len(body).to_bytes(4, "big") + body).hexdigest()
    else:
        raise KeyringError(f"unsupported key version {version}")
    return digest.upper()


def primary_fingerprints(data: bytes) -> list[str]:
    return [fingerprint(body) for tag, body in packets(dearmor(data)) if tag == PUBLIC_KEY]


def normalise(value: str) -> str:
    return "".join(value.split()).upper()
