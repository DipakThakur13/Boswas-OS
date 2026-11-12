"""Long operations: what is run, how its output is read, and what it means (no Qt, no I/O).

Two programs run long operations for Control Center, both through
commands.CommandRunner.stream():

  boswas-sysops    /usr/libexec/boswas/sysops <operation> ..., started with
                   polkit authorization (package management, cleanup,
                   firmware report)
  boswas-android   boswas-android --json <command> ... (the Android runtime;
                   it passes boswas-sysops' output through for privileged steps)

Both print JSON Lines (docs/architecture/platform-contracts.md, section 1):
``start``, ``progress``, ``log`` and exactly one final ``result`` line. A
boswas-android command that is not a privileged step may print one JSON
document with a ``schema`` instead. Lines that are not JSON (for example
the authorization tool's own messages) are kept as text for the error details.

Success is reported ONLY when a ``result`` line says ``ok: true`` and the
program exited with status 0. Everything else is a failure with a message
for the user: authorization cancelled (exit status 126/127), a result code of the
contract, or a program that ended without a result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import viewmodel as vm

SYSOPS, ANDROID = "sysops", "android"
SCHEMA = "boswas-sysops/1"

# Result codes of the contract and whether the user can usefully try again.
CODES = frozenset({"OK", "FAILED", "INVALID_ARGUMENT", "NOT_ALLOWED", "NOT_FOUND", "UNTRUSTED", "DISK_SPACE",
                   "LOCKED", "CANCELLED", "NETWORK", "UNSUPPORTED"})
RETRYABLE = frozenset({"NETWORK", "LOCKED"})
AUTH_DENIED, MALFORMED, TIMEOUT = "AUTH_DENIED", "MALFORMED", "TIMEOUT"
AUTH_MESSAGE = "Authorization was cancelled or denied. Nothing was changed."
CODE_HINTS = {
    "LOCKED": "Another program is installing or updating software right now. Wait for it to finish, then try "
              "again.",
    "NETWORK": "A download failed. Check the network connection, then try again.",
    "DISK_SPACE": "There is not enough free disk space. Free some space (Storage page), then try again.",
    "CANCELLED": "Cancelled. Nothing was changed.",
}
PHASE_LABELS = {"refresh": "Refreshing package lists", "download": "Downloading", "install": "Installing",
                "configure": "Configuring", "remove": "Removing", "cleanup": "Cleaning up", "verify": "Verifying",
                "init": "Setting up", "service": "Changing the service", "inspect": "Inspecting"}
MAX_TEXT_LINES = 40


@dataclass(frozen=True)
class JobSpec:
    """One operation: which program, which words, how it is shown."""
    kind: str                       # SYSOPS | ANDROID
    words: tuple[str, ...]          # sysops: (operation, *args); android: (command words, argument)
    title: str                      # "Installing updates"
    origin: str = ""                # page key that started it
    timeout: float = 4 * 3600.0


@dataclass(frozen=True)
class Event:
    kind: str                       # start | progress | log | result | document | text
    phase: str = ""
    percent: float | None = None
    message: str = ""
    level: str = "info"
    cancellable: bool | None = None
    ok: bool | None = None
    code: str = ""
    data: dict = field(default_factory=dict)


def _percent(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
        return float(value)
    return None


def parse_line(line: object) -> Event:
    """One output line as an Event; anything that is not a JSON object of the contract is text."""
    text = line if isinstance(line, str) else ""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return Event("text", message=vm.one_line(stripped, 400))
    try:
        doc = json.loads(stripped)
    except ValueError:
        return Event("text", message=vm.one_line(stripped, 400))
    if not isinstance(doc, dict):
        return Event("text", message=vm.one_line(stripped, 400))
    kind = doc.get("type")
    cancellable = doc.get("cancellable") if isinstance(doc.get("cancellable"), bool) else None
    message = vm.one_line(doc.get("message"), 400)
    if kind == "start":
        return Event("start", message=vm.one_line(doc.get("operation"), 60), cancellable=cancellable)
    if kind == "progress":
        phase = doc.get("phase") if isinstance(doc.get("phase"), str) else ""
        return Event("progress", phase=vm.one_line(phase, 20), percent=_percent(doc.get("percent")),
                     message=message, cancellable=cancellable)
    if kind == "log":
        level = "warning" if doc.get("level") == "warning" else "info"
        return Event("log", message=message, level=level)
    if kind == "result":
        ok = doc.get("ok") if isinstance(doc.get("ok"), bool) else None
        code = doc.get("code") if isinstance(doc.get("code"), str) else ""
        data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
        return Event("result", ok=ok, code=vm.one_line(code, 40), message=message, data=data)
    if kind is None and isinstance(doc.get("schema"), str):
        return Event("document", data=doc, message=message)
    return Event("text", message=vm.one_line(stripped, 400))


@dataclass
class JobState:
    """What the window knows about a running operation, updated event by event."""
    cancellable: bool = False
    phase: str = ""
    percent: float | None = None
    message: str = ""
    result: Event | None = None
    document: dict | None = None
    warnings: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    malformed_result: bool = False

    def feed(self, event: Event) -> None:
        if event.kind == "start":
            self.cancellable = bool(event.cancellable)
            if event.message:
                self.message = ""
        elif event.kind == "progress":
            if event.cancellable is not None:
                self.cancellable = event.cancellable
            self.phase = event.phase or self.phase
            self.percent = event.percent
            self.message = event.message or PHASE_LABELS.get(event.phase, self.message)
        elif event.kind == "log":
            if event.level == "warning" and event.message:
                self.warnings.append(event.message)
                self.warnings[:] = self.warnings[-10:]
        elif event.kind == "result":
            if self.result is None:
                self.result = event
                if event.ok is None or event.code not in CODES:
                    self.malformed_result = True
            else:
                self.malformed_result = True             # a second result line breaks the contract
            self.cancellable = False
        elif event.kind == "document":
            self.document = event.data
        elif event.kind == "text" and event.message:
            self.text.append(event.message)
            self.text[:] = self.text[-MAX_TEXT_LINES:]

    @property
    def progress_text(self) -> str:
        label = self.message or PHASE_LABELS.get(self.phase, "Working")
        return f"{label} ({int(self.percent)}%)" if self.percent is not None else label


@dataclass(frozen=True)
class Outcome:
    ok: bool
    code: str
    message: str
    retryable: bool = False
    detail: str = ""
    data: dict = field(default_factory=dict)

    @property
    def auth_denied(self) -> bool:
        return self.code == AUTH_DENIED


def _detail(state: JobState) -> str:
    return vm.one_line(" ".join(state.text[-3:]), 400)


def android_error(doc: dict) -> tuple[str, str, str] | None:
    """(code, message, problems) of a boswas-android error document, None when it is not one.

    boswas-android prints {"schema", "error": {"code", "message", "problems"}}; a
    plain string under "error" is accepted too.
    """
    error = doc.get("error")
    if isinstance(error, dict):
        problems = [vm.sentence(p, 300) for p in error.get("problems") or [] if isinstance(p, str) and p.strip()]
        return (vm.one_line(error.get("code"), 40) or "FAILED", vm.sentence(error.get("message"), 600),
                " ".join(problems))
    if isinstance(error, str) and error.strip():
        return "FAILED", vm.sentence(error, 600), ""
    if doc.get("ok") is False:
        return vm.one_line(doc.get("code"), 40) or "FAILED", vm.sentence(doc.get("message"), 600), ""
    return None


ANDROID_DONE = {"installed": "installed", "removed": "removed", "launched": "started"}


def android_success(doc: dict) -> str:
    """A sentence for a boswas-android success document ({"installed": {...}}, {"removed": ...}, ...)."""
    for key, verb in ANDROID_DONE.items():
        part = doc.get(key)
        if isinstance(part, dict):
            message = vm.one_line(part.get("message"), 400)
            name = vm.one_line(part.get("label") or part.get("package"), 120)
            return message or (f"{name} {verb}." if name else "Done.")
    return vm.one_line(doc.get("message"), 400) or "Done."


def outcome(state: JobState, exit_code: int | None, kind: str = SYSOPS) -> Outcome:
    """The meaning of a finished operation (exit_code None: it never ran to the end)."""
    result = state.result
    detail = _detail(state)
    if result is not None and not state.malformed_result:
        data = result.data
        result_detail = vm.one_line(data.get("detail"), 400) if isinstance(data.get("detail"), str) else ""
        if result.ok and result.code == "OK" and exit_code == 0:
            return Outcome(True, "OK", vm.one_line(result.message, 400) or "Done.", data=data)
        if result.ok:
            return Outcome(False, MALFORMED, f"The operation reported success but ended with exit code {exit_code}; "
                                             "treating it as failed.", detail=detail, data=data)
        message = vm.one_line(result.message, 400) or CODE_HINTS.get(result.code, "The operation failed.")
        hint = CODE_HINTS.get(result.code)
        if hint and hint not in message and result.code != "CANCELLED":
            message = f"{message} {hint}"
        return Outcome(False, result.code, message, result.code in RETRYABLE, result_detail or detail, data)
    if exit_code in (126, 127) and result is None:
        return Outcome(False, AUTH_DENIED, AUTH_MESSAGE, detail=detail)
    if state.malformed_result:
        return Outcome(False, MALFORMED, "The operation returned an answer Control Center does not understand. "
                                         "Nothing is reported as done.", detail=detail)
    if kind == ANDROID and state.document is not None:
        doc = state.document
        error = android_error(doc)
        if exit_code == 0 and error is None:
            return Outcome(True, "OK", android_success(doc), data=doc)
        code, message, problems = error or ("FAILED", "", "")
        return Outcome(False, code, message or "The Android runtime manager reported an error.",
                       code in RETRYABLE, problems or detail, doc)
    if exit_code is None:
        return Outcome(False, MALFORMED, "The operation stopped before it reported a result.", detail=detail)
    return Outcome(False, MALFORMED, f"The operation ended (exit code {exit_code}) without reporting a result, so "
                                     "it is not reported as done.", detail=detail)
