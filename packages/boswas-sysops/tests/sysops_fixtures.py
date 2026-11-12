"""Shared fakes of the boswas-sysops tests.

The real operations run everywhere; only their boundaries are replaced:

  FakeRunner   records every argv instead of starting a process, answers
               run() from handlers and plays scripted output, Status-Fd
               lines and cancel requests through stream()
  make_ctx()   a Context over a temporary root directory, an Emitter
               writing into a buffer, and a CancelRequest

The tests add the package to sys.path, so they run without PYTHONPATH and
without root.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

from boswas_sysops import runner as runner_module  # noqa: E402
from boswas_sysops.context import CancelRequest, Context  # noqa: E402
from boswas_sysops.protocol import Emitter  # noqa: E402
from boswas_sysops.rootfs import Root  # noqa: E402
from boswas_sysops.runner import Completed, Streamed  # noqa: E402

KERNEL = "6.12.111+deb13-amd64"
FLATHUB_REPO = PACKAGE / "data" / "flatpak-remotes" / "flathub.flatpakrepo"
FLATHUB_FINGERPRINT = "6E5C05D979C76DAF93C081354184DD4D907A7CAE"


def flathub_key() -> bytes:
    """A real OpenPGP public key (Flathub's, shipped in data/) for keyring tests."""
    import base64
    for line in FLATHUB_REPO.read_text(encoding="utf-8").splitlines():
        if line.startswith("GPGKey="):
            return base64.b64decode(line.split("=", 1)[1])
    raise AssertionError("no GPGKey in flathub.flatpakrepo")


class FakeRunner:
    """run(): first handler whose predicate matches the argv answers; stream(): next script."""

    def __init__(self, cancel: CancelRequest | None = None):
        self.calls: list[tuple[str, list[str]]] = []
        self.handlers: list[tuple[object, object]] = []
        self.scripts: list[dict] = []
        self.cancel = cancel

    def on(self, predicate, response) -> None:
        """predicate: a tuple of tokens that must all appear in argv (in order not required), or a callable."""
        self.handlers.append((predicate, response))

    def script(self, events=(), code: int = 0) -> None:
        self.scripts.append({"events": list(events), "code": code})

    @staticmethod
    def _matches(predicate, command) -> bool:
        if callable(predicate):
            return predicate(command)
        return all(token in command for token in predicate)

    def available(self, path: str) -> bool:
        return path in runner_module.ALLOWED_PATHS

    def run(self, command, timeout: float = 60.0) -> Completed:
        runner_module._check(command)
        assert timeout and timeout > 0
        self.calls.append(("run", list(command)))
        for predicate, response in self.handlers:
            if self._matches(predicate, command):
                return response(command) if callable(response) else response
        return Completed(0, "", "")

    def stream(self, command, *, timeout, on_line=None, on_status=None, poll=None) -> Streamed:
        runner_module._check(command)
        assert timeout and timeout > 0
        if any(runner_module.STATUS_FD_TOKEN in a for a in command):
            assert on_status is not None, "Status-Fd placeholder without a status callback"
        self.calls.append(("stream", list(command)))
        script = self.scripts.pop(0) if self.scripts else {"events": [], "code": 0}
        tail = []
        for event in script["events"]:
            kind = event[0]
            if kind == "line":
                tail.append(event[1])
                if on_line:
                    on_line(event[1])
            elif kind == "status":
                if on_status:
                    on_status(event[1])
            elif kind == "cancel":
                self.cancel.request()
            if poll is not None and poll():
                return Streamed(-2, cancelled=True, tail=tail)
        return Streamed(script["code"], tail=tail)

    def commands(self, kind: str | None = None) -> list[list[str]]:
        return [c for k, c in self.calls if kind is None or k == kind]


class Harness:
    """A temporary root, a fake runner and a context; events() parses what was emitted."""

    def __init__(self, cancellable: bool = True, uid: int = 1000):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Root(self.tmp.name)
        self.cancel = CancelRequest()
        self.runner = FakeRunner(self.cancel)
        self.buffer = io.StringIO()
        self.emitter = Emitter(self.buffer)
        self.ctx = Context(self.root, self.runner, self.emitter, self.cancel, uid, KERNEL, cancellable)

    def path(self, path: str) -> str:
        return self.root(path)

    def write(self, path: str, text: str | bytes, mode: int = 0o644) -> str:
        target = self.root(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(text if isinstance(text, bytes) else text.encode("utf-8"))
        os.chmod(target, mode)
        return target

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.buffer.getvalue().splitlines() if line.strip()]

    def kinds(self, kind: str) -> list[dict]:
        return [e for e in self.events() if e["type"] == kind]

    def close(self) -> None:
        self.tmp.cleanup()
