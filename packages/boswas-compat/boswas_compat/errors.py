"""Errors with stable exit codes (documented in docs/administration/cli.md)."""

from __future__ import annotations

EXIT_OK = 0
EXIT_FAILED = 1          # the operation ran and failed (installer error, ...)
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3       # application, installer or manifest not found
EXIT_REFUSED = 4         # refused by policy or by a safety rule (root, blocked, ...)
EXIT_UNAVAILABLE = 5     # runtime unavailable (Wine, bubblewrap, AppArmor confinement)
EXIT_BUSY = 6            # the application is running or locked by another operation
EXIT_SOFTWARE = 70       # internal error


class WinAppError(Exception):
    """Base class; carries the exit code and a machine-readable reason."""

    exit_code = EXIT_FAILED
    reason = "failed"

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(message)
        if reason:
            self.reason = reason


class UsageError(WinAppError):
    exit_code = EXIT_USAGE
    reason = "usage"


class NotFound(WinAppError):
    exit_code = EXIT_NOT_FOUND
    reason = "not-found"


class Refused(WinAppError):
    exit_code = EXIT_REFUSED
    reason = "refused"


class Unavailable(WinAppError):
    exit_code = EXIT_UNAVAILABLE
    reason = "unavailable"


class Busy(WinAppError):
    exit_code = EXIT_BUSY
    reason = "busy"


class ManifestError(WinAppError):
    """A manifest does not conform to the manifest format."""

    exit_code = EXIT_FAILED
    reason = "invalid-manifest"

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or []
