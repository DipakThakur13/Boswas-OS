"""Errors with stable exit codes and machine-readable codes.

Exit codes (docs/architecture/platform-contracts.md, section 2):

  0  done
  1  the operation ran and failed
  2  invalid argument, or refused (incompatible APK, policy, confirmation missing)
  3  the Android runtime is not installed, not initialised or not running
  6  this computer or session cannot run the Android runtime

Commands that hand a step to boswas-sysops (runtime, service) exit with
boswas-sysops's own status instead (126/127: not authorised by pkexec).
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_RUNTIME = 3
EXIT_UNSUPPORTED = 6
EXIT_INTERNAL = 70


class AndroidError(Exception):
    """Base class: ``message`` is written for the user; ``code`` is stable."""

    exit_code = EXIT_FAILED
    code = "FAILED"

    def __init__(self, message: str, *, code: str | None = None, problems: list[str] | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.problems = list(problems or [])


class Failed(AndroidError):
    pass


class InvalidArgument(AndroidError):
    exit_code = EXIT_INVALID
    code = "INVALID_ARGUMENT"


class ApkError(InvalidArgument):
    """The file is not an APK this tool can read (never a traceback)."""

    code = "INVALID_APK"


class Refused(AndroidError):
    exit_code = EXIT_INVALID
    code = "NOT_ALLOWED"


class RuntimeMissing(AndroidError):
    exit_code = EXIT_RUNTIME
    code = "RUNTIME_NOT_INSTALLED"


class NotInitialized(AndroidError):
    exit_code = EXIT_RUNTIME
    code = "RUNTIME_NOT_INITIALIZED"


class NotRunning(AndroidError):
    exit_code = EXIT_RUNTIME
    code = "SESSION_NOT_RUNNING"


class Unsupported(AndroidError):
    exit_code = EXIT_UNSUPPORTED
    code = "UNSUPPORTED"
