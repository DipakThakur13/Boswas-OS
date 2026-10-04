"""Errors the window explains to the user.

Every failure a backend call can have ends up as one of these, each with a
message written for the user (never a traceback). The widgets show
``user_message(exc)``.
"""

from __future__ import annotations


class ControlCenterError(Exception):
    """Base class: ``message`` is shown to the user as plain text."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class Unavailable(ControlCenterError):
    """A service is not running or a program is not installed (not an error of the device)."""


class ServiceUnavailable(Unavailable):
    def __init__(self, service: str, message: str):
        super().__init__(message)
        self.service = service


class CommandUnavailable(Unavailable):
    def __init__(self, program: str, message: str | None = None):
        super().__init__(message or f"{program} is not installed on this system.")
        self.program = program


class Failed(ControlCenterError):
    """A service or program answered with an error."""


class ServiceRefused(Failed):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CommandFailed(Failed):
    def __init__(self, program: str, message: str, code: int | None = None):
        super().__init__(message)
        self.program, self.code = program, code


def user_message(exc: BaseException) -> str:
    if isinstance(exc, ControlCenterError):
        return exc.message
    return f"Unexpected error ({type(exc).__name__}): {str(exc)[:300]}"


def is_unavailable(exc: BaseException) -> bool:
    return isinstance(exc, Unavailable)
