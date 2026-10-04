"""Boswas WinCompat: isolated Windows applications on Boswas OS (boswas-winapp)."""

__version__ = "1.0~alpha3"

# Identifier of the JSON documents printed with --json. Bump the major number
# on any backwards-incompatible change to field names or meaning.
JSON_SCHEMA = "boswas-winapp/1"

# Identifier of the per-application installation record (app.json).
RECORD_SCHEMA = "boswas-winapp-record/1"

# Version of the compatibility manifest format this release reads.
MANIFEST_SCHEMA_VERSION = 1

# Boswas OS v1 runs x86_64 (64-bit) Windows applications only. This is a
# product decision (ADR-0014), not a missing feature: there is no 32-bit
# Wine, no WoW64 and no fallback. Every refusal of 32-bit software uses this
# sentence, in the CLI, the Compatibility Manager and the device agent.
UNSUPPORTED_32BIT_MESSAGE = ("This application requires 32-bit Windows compatibility, "
                             "which is not supported by Boswas OS.")
