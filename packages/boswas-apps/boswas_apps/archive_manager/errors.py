"""The errors of the archive engine. Every message is a sentence that can be shown to the user. No Qt."""

from __future__ import annotations


class ArchiveError(Exception):
    """Something about an archive that is told to the user in plain words."""


class UnsupportedFormat(ArchiveError):
    """A file this system cannot open as an archive. ``needs`` names the missing package, if one would help."""

    def __init__(self, message: str, needs: str = "") -> None:
        super().__init__(message)
        self.needs = needs


class PasswordRequired(ArchiveError):
    """The data is encrypted and no password was given."""

    def __init__(self, message: str = "A password is needed to read this archive.") -> None:
        super().__init__(message)


class WrongPassword(PasswordRequired):
    """The password given does not open the data."""

    def __init__(self, message: str = "That password is not correct.") -> None:
        super().__init__(message)


class Cancelled(Exception):
    """The user stopped the operation. Not an error: nothing is reported."""
