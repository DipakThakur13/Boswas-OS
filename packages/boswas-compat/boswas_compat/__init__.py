"""Boswas WinCompat: isolated Windows applications on Boswas OS (boswas-winapp)."""

__version__ = "1.0~alpha3"

# Identifier of the JSON documents printed with --json. Bump the major number
# on any backwards-incompatible change to field names or meaning.
JSON_SCHEMA = "boswas-winapp/1"

# Identifier of the per-application installation record (app.json).
RECORD_SCHEMA = "boswas-winapp-record/1"

# Version of the compatibility manifest format this release reads.
MANIFEST_SCHEMA_VERSION = 1

# Boswas OS runs x86_64 (64-bit) and x86 (32-bit) Windows applications
# (ADR-0022): 32-bit programs use Debian's wine32 from the i386 architecture.
# When that runtime is not installed (an administrator removed it), every
# refusal of 32-bit software uses this sentence, in the CLI, the
# Compatibility Manager and the device agent.
UNSUPPORTED_32BIT_MESSAGE = ("This application requires 32-bit Windows compatibility, "
                             "which is not installed on this system.")

# Display names of the Windows architectures.
ARCHITECTURE_NAMES = {"x86_64": "x86_64 (64-bit)", "x86": "x86 (32-bit)"}
