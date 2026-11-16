"""A very small D-Bus client for the tests (standard library only).

It speaks the wire protocol directly, so a test sees exactly which types a
service sends: a variant comes back as ``Variant(signature, value)``. Only
what the MPRIS tests need is here: EXTERNAL authentication on a Unix
socket, method calls, signal matching, and the basic and container types.
The socket is non-blocking; ``pump`` is called while waiting so that the
service under test (which lives in the same process) can answer.
"""

from __future__ import annotations

import os
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable

_ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8, "s": 4, "o": 4, "g": 1, "a": 4,
          "v": 1, "(": 8, "{": 8}
_PACK = {"y": "B", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d"}


@dataclass
class Variant:
    signature: str
    value: object


@dataclass
class Message:
    kind: int                     # 1 call, 2 reply, 3 error, 4 signal
    fields: dict
    signature: str
    body: list

    @property
    def is_error(self) -> bool:
        return self.kind == 3

    @property
    def error_name(self) -> str:
        return self.fields.get(4, "")


def split_signature(signature: str) -> list[str]:
    """The complete types of a signature: ``"sa{sv}as"`` -> ``["s", "a{sv}", "as"]``."""
    parts, index = [], 0
    while index < len(signature):
        start = index
        while signature[index] == "a":
            index += 1
        if signature[index] in "({":
            close = ")" if signature[index] == "(" else "}"
            opener, depth = signature[index], 0
            while True:
                if signature[index] == opener:
                    depth += 1
                elif signature[index] == close:
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
        index += 1
        parts.append(signature[start:index])
    return parts


class _Writer:
    def __init__(self) -> None:
        self.data = bytearray()

    def align(self, boundary: int) -> None:
        self.data += b"\0" * (-len(self.data) % boundary)

    def write(self, signature: str, value) -> None:
        kind = signature[0]
        self.align(_ALIGN[kind])
        if kind in _PACK:
            self.data += struct.pack("<" + _PACK[kind], value)
        elif kind == "b":
            self.data += struct.pack("<I", 1 if value else 0)
        elif kind in "so":
            raw = value.encode("utf-8")
            self.data += struct.pack("<I", len(raw)) + raw + b"\0"
        elif kind == "g":
            raw = value.encode("ascii")
            self.data += bytes([len(raw)]) + raw + b"\0"
        elif kind == "v":
            self.write("g", value.signature)
            self.write(value.signature, value.value)
        elif kind == "a":
            element = signature[1:]
            self.data += b"\0\0\0\0"
            length_at = len(self.data) - 4
            self.align(_ALIGN[element[0]])
            start = len(self.data)
            items = value.items() if element[0] == "{" else value
            for item in items:
                self.write(element, item)
            struct.pack_into("<I", self.data, length_at, len(self.data) - start)
        elif kind in "({":
            for part, item in zip(split_signature(signature[1:-1]), value):
                self.write(part, item)
        else:
            raise ValueError(f"cannot write {signature!r}")


class _Reader:
    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data, self.offset = data, offset

    def align(self, boundary: int) -> None:
        self.offset += -self.offset % boundary

    def read(self, signature: str):
        kind = signature[0]
        self.align(_ALIGN[kind])
        if kind in _PACK:
            size = struct.calcsize(_PACK[kind])
            value = struct.unpack_from("<" + _PACK[kind], self.data, self.offset)[0]
            self.offset += size
            return value
        if kind == "b":
            value = struct.unpack_from("<I", self.data, self.offset)[0]
            self.offset += 4
            return bool(value)
        if kind in "so":
            length = struct.unpack_from("<I", self.data, self.offset)[0]
            value = self.data[self.offset + 4:self.offset + 4 + length].decode("utf-8")
            self.offset += 4 + length + 1
            return value
        if kind == "g":
            length = self.data[self.offset]
            value = self.data[self.offset + 1:self.offset + 1 + length].decode("ascii")
            self.offset += length + 2
            return value
        if kind == "v":
            inner = self.read("g")
            return Variant(inner, self.read(inner))
        if kind == "a":
            element = signature[1:]
            length = struct.unpack_from("<I", self.data, self.offset)[0]
            self.offset += 4
            self.align(_ALIGN[element[0]])
            end = self.offset + length
            items = []
            while self.offset < end:
                items.append(self.read(element))
            return dict(items) if element[0] == "{" else items
        if kind in "({":
            return tuple(self.read(part) for part in split_signature(signature[1:-1]))
        raise ValueError(f"cannot read {signature!r}")


class Client:
    """One connection to a bus. ``pump()`` is called while waiting for an answer."""

    def __init__(self, address: str, pump: Callable[[], None]) -> None:
        self.pump = pump
        self.serial = 0
        self.buffer = b""
        self.signals: list[Message] = []
        options = dict(part.split("=", 1) for part in address.split(":", 1)[1].split(","))
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(options["path"] if "path" in options else "\0" + options["abstract"])
        self.socket.sendall(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode() + b"\r\n")
        answer = b""
        while not answer.endswith(b"\r\n"):
            answer += self.socket.recv(1)
        if not answer.startswith(b"OK"):
            raise ConnectionError(f"the bus refused the connection: {answer!r}")
        self.socket.sendall(b"BEGIN\r\n")
        self.socket.setblocking(False)
        self.name = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "Hello").body[0]

    def close(self) -> None:
        self.socket.close()

    def _send(self, kind: int, fields: dict, signature: str, arguments: tuple) -> int:
        body = _Writer()
        for part, value in zip(split_signature(signature), arguments):
            body.write(part, value)
        if signature:
            fields[8] = Variant("g", signature)
        self.serial += 1
        header = _Writer()
        header.data += b"l" + bytes([kind, 0, 1]) + struct.pack("<II", len(body.data), self.serial)
        header.write("a(yv)", list(fields.items()))
        header.align(8)
        self.socket.setblocking(True)
        self.socket.sendall(bytes(header.data + body.data))
        self.socket.setblocking(False)
        return self.serial

    def _receive(self) -> Message | None:
        try:
            self.buffer += self.socket.recv(65536)
        except BlockingIOError:
            pass
        if len(self.buffer) < 16:
            return None
        body_length, _serial, fields_length = struct.unpack_from("<III", self.buffer, 4)
        header_end = 16 + fields_length
        header_end += -header_end % 8
        total = header_end + body_length
        if len(self.buffer) < total:
            return None
        data, self.buffer = self.buffer[:total], self.buffer[total:]
        fields = {code: variant.value for code, variant in _Reader(data, 12).read("a(yv)")}
        signature = fields.get(8, "")
        reader = _Reader(data, header_end)
        return Message(data[1], fields, signature, [reader.read(part) for part in split_signature(signature)])

    def process(self) -> None:
        """Read what has arrived; signals are collected in ``signals``."""
        while (message := self._receive()) is not None:
            if message.kind == 4:
                self.signals.append(message)

    def call(self, destination: str, path: str, interface: str, member: str, signature: str = "",
             *arguments, timeout: float = 5.0) -> Message:
        fields = {1: Variant("o", path), 2: Variant("s", interface), 3: Variant("s", member),
                  6: Variant("s", destination)}
        serial = self._send(1, fields, signature, arguments)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            while (message := self._receive()) is not None:
                if message.kind == 4:
                    self.signals.append(message)
                elif message.fields.get(5) == serial:
                    return message
            time.sleep(0.001)
        raise TimeoutError(f"{interface}.{member} was not answered")

    def match_signals(self, sender: str, path: str) -> None:
        self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s",
                  f"type='signal',sender='{sender}',path='{path}'")

    def take_signals(self, member: str) -> list[Message]:
        """The collected signals called ``member`` (they are removed from ``signals``)."""
        self.pump()
        self.process()
        taken = [message for message in self.signals if message.fields.get(3) == member]
        self.signals = [message for message in self.signals if message.fields.get(3) != member]
        return taken


def plain(value):
    """A received value without its ``Variant`` wrappers."""
    if isinstance(value, Variant):
        return plain(value.value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value
