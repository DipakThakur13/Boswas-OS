"""Privileged steps: `pkexec /usr/libexec/boswas/sysops android-*`, its JSON Lines passed through.

boswas-sysops (docs/architecture/platform-contracts.md, section 1) prints
JSON Lines: start, progress, log and exactly one final result. This module
runs it through the allowlisted runner, hands every line to the caller
(unchanged when it is JSON; non-JSON output, e.g. from pkexec itself, is
wrapped as a log line) and forwards cancel requests to its stdin.

When pkexec refuses (126 authentication dismissed, 127 not authorised) or the
helper is missing, no result line can come from the helper; a result is then
synthesised here so a JSON consumer always sees exactly one result line:
code NOT_AUTHORIZED or UNAVAILABLE (codes of boswas-android, not of sysops).
"""

from __future__ import annotations

import json
import threading

from . import runner as runner_mod

SCHEMA = "boswas-sysops/1"
NOT_AUTHORIZED_EXITS = (126, 127)


def _result(code: str, message: str, ok: bool = False) -> dict:
    return {"type": "result", "ok": ok, "code": code, "message": message, "data": {}}


class Outcome:
    def __init__(self):
        self.exit_code = 1
        self.result: dict | None = None
        self.lines: list[dict] = []


def run(the_runner, operation: str, args: tuple[str, ...] = (), on_event=None,
        cancel_event: threading.Event | None = None) -> Outcome:
    """Run one android-* operation; on_event(dict) receives every event, in order."""
    outcome = Outcome()

    def emit(event: dict) -> None:
        outcome.lines.append(event)
        if event.get("type") == "result" and outcome.result is None:
            outcome.result = event
        if on_event is not None:
            on_event(event)

    def on_line(text: str) -> None:
        stripped = text.strip()
        if not stripped:
            return
        try:
            event = json.loads(stripped)
        except ValueError:
            event = None
        if not isinstance(event, dict) or "type" not in event:
            event = {"type": "log", "level": "info", "message": stripped[:1000]}
        emit(event)

    command = runner_mod.sysops_argv(operation, *args)
    try:
        code = the_runner.stream(command, on_line, cancel_event)
    except runner_mod.RunnerError as exc:
        emit(_result("UNAVAILABLE", f"The Boswas system helper could not be run: {exc.message}"))
        outcome.exit_code = 1
        return outcome
    outcome.exit_code = code
    if outcome.result is None:
        if code in NOT_AUTHORIZED_EXITS:
            emit(_result("NOT_AUTHORIZED", "Not authorised: the administrator password was not entered or the "
                                           f"action is not permitted (pkexec exit status {code})."))
        else:
            emit(_result("FAILED", f"The Boswas system helper ended without a result (exit status {code})."))
    return outcome
