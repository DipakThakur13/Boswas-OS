"""Entry point: privilege check, operation table, protocol, exit status.

    pkexec /usr/libexec/boswas/sysops <operation> [arguments...]

The operation table is the complete list of what the helper can do. Each
entry names its handler, whether it can be cancelled at all, and whether it
only reads (polkit allow_active=yes) or changes the system
(allow_active=auth_admin_keep); data/com.boswas.sysops.policy has one action
per entry and a test keeps both in step.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from . import android, apt, audit, firmware, flatpak, maintenance
from .context import CancelRequest, Context, watch_stdin
from .protocol import EXIT_CODES, Emitter, SysopsError
from .rootfs import Root
from .runner import SAFE_ENV, Runner


@dataclass(frozen=True)
class Operation:
    handler: object
    cancellable: bool
    read_only: bool
    description: str          # polkit action description
    message: str              # polkit authentication message


OPERATIONS = {
    "apt-refresh": Operation(apt.refresh, True, False, "Refresh the list of available software and updates",
                             "Authentication is required to refresh the list of available software."),
    "apt-upgrade": Operation(apt.upgrade, True, False, "Install software updates",
                             "Authentication is required to install software updates."),
    "apt-install": Operation(apt.install, True, False, "Install software from the configured repositories",
                             "Authentication is required to install software."),
    "apt-remove": Operation(apt.remove, False, False, "Remove installed software",
                            "Authentication is required to remove software."),
    "apt-install-local": Operation(apt.install_local, True, False, "Install a software package from a local file",
                                   "Authentication is required to install a package from a file. Its origin is "
                                   "not verified by a signed repository."),
    "apt-clean": Operation(apt.clean, False, False, "Remove downloaded software package files",
                           "Authentication is required to clean the software package cache."),
    "journal-vacuum": Operation(maintenance.journal_vacuum, False, False, "Reduce the size of the system journal",
                                "Authentication is required to clean the system journal."),
    "firmware-report": Operation(firmware.report, False, True, "Report missing hardware firmware",
                                 "Authentication is required to read the kernel messages."),
    "flatpak-remote-enable": Operation(flatpak.enable, False, False, "Enable an approved Flatpak remote",
                                       "Authentication is required to add a Flatpak remote."),
    "flatpak-remote-disable": Operation(flatpak.disable, False, False, "Disable a Flatpak remote",
                                        "Authentication is required to remove a Flatpak remote."),
    "android-runtime-install": Operation(android.runtime_install, True, False, "Install the Android runtime",
                                         "Authentication is required to install the Android runtime."),
    "android-runtime-remove": Operation(android.runtime_remove, False, False, "Remove the Android runtime",
                                        "Authentication is required to remove the Android runtime."),
    "android-init": Operation(android.init, True, False, "Download and set up the Android system",
                              "Authentication is required to set up the Android system."),
    "android-update": Operation(android.update, True, False, "Update the Android system",
                                "Authentication is required to update the Android system."),
    "android-repair": Operation(android.repair, False, False, "Repair the Android runtime",
                                "Authentication is required to repair the Android runtime."),
    "android-service": Operation(android.service, False, True, "Start or stop the Android runtime",
                                 "Authentication is required to start or stop the Android runtime."),
}


def requesting_uid(environ) -> int | None:
    value = environ.get("PKEXEC_UID", "")
    return int(value) if value.isdigit() else None


def main(argv: list[str] | None = None, *, ctx: Context | None = None, euid: int | None = None,
         environ=None, stdin=None, stdout=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if environ is None else environ
    emitter = ctx.emit if ctx is not None else Emitter(stdout or sys.stdout)
    uid = requesting_uid(environ)
    euid = os.geteuid() if euid is None else euid
    if euid != 0 or uid is None:
        emitter.result(False, "INVALID_ARGUMENT", "boswas-sysops runs only as root, started through pkexec.")
        return EXIT_CODES["INVALID_ARGUMENT"]
    if ctx is None:
        # Production: nothing of the caller's environment survives (pkexec already
        # cleared most of it); files are created with explicit modes.
        os.environ.clear()
        os.environ.update(SAFE_ENV)
        os.umask(0o022)
        ctx = Context(Root("/"), Runner(), emitter, CancelRequest(), uid, os.uname().release)
    if not argv or argv[0] not in OPERATIONS:
        name = argv[0] if argv else ""
        emitter.result(False, "INVALID_ARGUMENT", f"Unknown operation {name[:60]!r}.",
                       {"operations": sorted(OPERATIONS)})
        audit.record(ctx.root, uid, name[:60] or "-", [], "INVALID_ARGUMENT")
        return EXIT_CODES["INVALID_ARGUMENT"]
    name, args = argv[0], argv[1:]
    operation = OPERATIONS[name]
    ctx.cancellable = operation.cancellable
    emitter.start(name, operation.cancellable)
    if operation.cancellable and stdin is not False:
        watch_stdin(stdin if stdin is not None else sys.stdin.buffer, ctx.cancel)
    code = "FAILED"
    try:
        message, data = operation.handler(ctx, args)
        code = "OK"
        emitter.result(True, "OK", message, data)
    except SysopsError as exc:
        code = exc.code
        emitter.result(False, exc.code, exc.message, exc.data)
    except Exception as exc:          # never a traceback on the protocol stream
        code = "FAILED"
        emitter.result(False, "FAILED", f"Unexpected error in boswas-sysops ({type(exc).__name__}).",
                       {"detail": str(exc)[:500]})
    finally:
        audit.record(ctx.root, uid, name, [a for a in args if isinstance(a, str)], code)
    return EXIT_CODES[code]
