"""F-Droid client download: HTTPS only, size-bounded, then the signer certificate must match.

F-Droid is offered only when the user asks for it, under its own name (it
is an independent catalogue of free and open-source apps, not Google Play).
The download URL and the expected signing-certificate SHA-256 come from the
shipped runtime.conf; any other certificate is refused and the file deleted.
Android verifies the APK signature itself when installing it.
"""

from __future__ import annotations

import os
import ssl
import tempfile
import urllib.request

from . import apk
from .errors import Failed, Refused

MAX_BYTES = 100 * 1024 * 1024
TIMEOUT = 60


def download(url: str, directory: str, opener=None) -> str:
    """Download url (https only) into a new file in directory; return its path."""
    if not url.startswith("https://"):
        raise Refused("The F-Droid download URL is not an https:// URL.", code="UNTRUSTED")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="fdroid-", suffix=".apk", dir=directory)
    try:
        if opener is None:
            context = ssl.create_default_context()
            handler = urllib.request.HTTPSHandler(context=context)
            opener = urllib.request.build_opener(handler).open
        with os.fdopen(fd, "wb") as out, opener(url, timeout=TIMEOUT) as response:
            final = getattr(response, "geturl", lambda: url)()
            if not str(final).startswith("https://"):
                raise Refused("The F-Droid download was redirected to a non-HTTPS address.", code="UNTRUSTED")
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise Failed("The F-Droid download is larger than expected; it was discarded.")
                out.write(chunk)
        return path
    except (OSError, ValueError) as exc:
        _discard(path)
        raise Failed(f"F-Droid could not be downloaded ({getattr(exc, 'reason', None) or exc}).",
                     code="NETWORK") from None
    except BaseException:
        _discard(path)
        raise


def verify(path: str, expected_sha256: str, runtime_abis: tuple[str, ...]) -> dict:
    """Inspect the download; refuse (and delete it) unless it is org.fdroid.fdroid signed by the expected certificate."""
    try:
        doc = apk.inspect(path, runtime_abis)
    except Exception:
        _discard(path)
        raise
    digests = {c["sha256"] for c in doc["signing"]["certificates"]}
    if doc["package"] != "org.fdroid.fdroid" or expected_sha256 not in digests:
        _discard(path)
        raise Refused("The downloaded F-Droid file is not signed by F-Droid's published certificate; it was "
                      "deleted and nothing was installed.", code="UNTRUSTED")
    return doc


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
