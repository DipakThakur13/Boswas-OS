"""Google Play on Boswas OS: the feasibility assessment shown to users.

Assessed for the Waydroid runtime in docs/android/README.md ("Google Play"):
there is no legitimate way for Boswas OS to offer Google Play, so it is never
installed, downloaded or offered, and the runtime is initialised with
LineageOS without Google applications (VANILLA). Alternatives are offered
under their own names.
"""

from __future__ import annotations

REASON = ("Google Play is not available on Boswas OS. Google Play and Google Mobile Services are licensed by Google "
          "only to device makers for Play Protect certified devices; Boswas OS has no such licence and its Android "
          "runtime is not certified.")

DETAILS = (
    "Google Mobile Services (Google Play, Play Services) are not part of open-source Android and are only "
    "available through a licence with Google, for devices Google certifies (Play Protect certified).",
    "Boswas OS has no such licence, and its Android runtime (Waydroid, LineageOS) is not Play Protect certified.",
    "The \"GAPPS\" images distributed for Waydroid contain Google's proprietary apps without a licence from Google, "
    "so they are not a legitimate source for Boswas OS to offer; Boswas OS never downloads them.",
    "Boswas OS does not work around Google's device certification, Play Integrity or attestation checks, and never "
    "asks for Google account credentials.",
    "Supported alternatives: F-Droid, an independent catalogue of free and open-source Android apps (it is not "
    "Google Play), and installing APK files you already have.",
)

ALTERNATIVES = ("fdroid", "local-apk")
ALTERNATIVE_LABELS = {"fdroid": "F-Droid (independent app store; not Google Play)",
                      "local-apk": "Install an APK file"}


def assessment() -> dict:
    """The google_play object of `status` (docs/architecture/platform-contracts.md)."""
    return {"available": False, "reason": REASON, "alternatives": list(ALTERNATIVES)}


def detailed() -> dict:
    doc = assessment()
    doc["details"] = list(DETAILS)
    doc["alternative_labels"] = dict(ALTERNATIVE_LABELS)
    return doc
