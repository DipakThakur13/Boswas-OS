"""Flatpak remotes: enable or disable a system-wide remote approved by Boswas.

Only remotes that are both shipped (a .flatpakrepo file in
/usr/share/boswas/sysops/flatpak-remotes/, which carries the remote's
signing key) and allowed by FLATPAK_REMOTES in /etc/boswas/apps.conf can be
enabled. Flatpak verifies everything it downloads from a remote with that
key. Applications are then installed per user (flatpak --user) without this
helper.
"""

from __future__ import annotations

import os
import stat

from . import policy, validate
from .context import Context
from .protocol import SysopsError
from .runner import EXECUTABLES, argv

REMOTES_DIR = "/usr/share/boswas/sysops/flatpak-remotes"
TIMEOUT = 300.0


def _require_flatpak(ctx: Context) -> None:
    if not ctx.root.executable(EXECUTABLES["flatpak"]):
        raise SysopsError("UNSUPPORTED", "Flatpak is not installed on this device.")


def _remote_file(ctx: Context, name: str) -> str:
    path = f"{REMOTES_DIR}/{name}.flatpakrepo"
    try:
        st = os.lstat(ctx.root(path))
    except OSError:
        raise SysopsError("NOT_ALLOWED", f"{name} is not a Flatpak remote approved by Boswas.") from None
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022 or (os.geteuid() == 0 and st.st_uid != 0):
        raise SysopsError("UNTRUSTED", f"The definition of the Flatpak remote {name} is not a protected system file.")
    return path


def enable(ctx: Context, args: list[str]) -> tuple[str, dict]:
    name = validate.remote_name(args)
    if name not in policy.flatpak_remotes(ctx.root):
        raise SysopsError("NOT_ALLOWED", f"The device policy does not allow the Flatpak remote {name} "
                                         f"(FLATPAK_REMOTES in {policy.APPS_CONF}).")
    path = _remote_file(ctx, name)
    _require_flatpak(ctx)
    ctx.emit.progress("configure", None, f"Adding the Flatpak remote {name}", cancellable=False)
    result = ctx.runner.run(argv("flatpak", "remote-add", "--system", "--if-not-exists", name, path), TIMEOUT)
    if result.code != 0:
        raise SysopsError("FAILED", f"Adding the Flatpak remote {name} failed (flatpak exit code {result.code}).",
                          {"detail": (result.stderr or result.stdout).strip()[-1000:]})
    return f"The Flatpak remote {name} is enabled.", {"remote": name}


def disable(ctx: Context, args: list[str]) -> tuple[str, dict]:
    name = validate.remote_name(args)
    _require_flatpak(ctx)
    ctx.emit.progress("configure", None, f"Removing the Flatpak remote {name}", cancellable=False)
    result = ctx.runner.run(argv("flatpak", "remote-delete", "--system", name), TIMEOUT)
    output = (result.stderr or "") + (result.stdout or "")
    if result.code != 0:
        if "not found" in output.lower() or "no remote" in output.lower():
            return f"The Flatpak remote {name} was not configured.", {"remote": name}
        raise SysopsError("FAILED", f"Removing the Flatpak remote {name} failed (flatpak exit code {result.code}). "
                                    "Applications installed from it may still need it.",
                          {"detail": output.strip()[-1000:]})
    return f"The Flatpak remote {name} is disabled.", {"remote": name}
