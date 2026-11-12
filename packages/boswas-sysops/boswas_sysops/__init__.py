"""boswas-sysops: the one privileged helper of Boswas OS.

A fixed set of system operations (APT, cleanup, firmware report, Flatpak
remotes, the Android runtime), each started as

    pkexec /usr/libexec/boswas/sysops <operation> [arguments...]

with its own polkit action (com.boswas.sysops.<operation>). Output is JSON
Lines (docs/architecture/platform-contracts.md, section 1). Never setuid,
never a daemon, never a shell, never an arbitrary command.
"""

__version__ = "1.0~alpha3"

# Identifier of the JSON Lines protocol. Bump the major number on any
# backwards-incompatible change to the event shapes or the result codes.
SCHEMA = "boswas-sysops/1"

# The installed helper: pkexec selects the polkit action by this path and the
# first argument (the operation).
HELPER_PATH = "/usr/libexec/boswas/sysops"

POLKIT_PREFIX = "com.boswas.sysops."
