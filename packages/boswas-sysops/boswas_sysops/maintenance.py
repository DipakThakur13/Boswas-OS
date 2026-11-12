"""journal-vacuum: shrink the systemd journal to at most 200 MB."""

from __future__ import annotations

from . import validate
from .context import Context
from .protocol import SysopsError
from .runner import argv

JOURNAL_DIRS = ("/var/log/journal", "/run/log/journal")
VACUUM_SIZE = "200M"
TIMEOUT = 600.0


def _journal_bytes(ctx: Context) -> int:
    return sum(ctx.root.tree_size(d) for d in JOURNAL_DIRS if ctx.root.exists(d))


def journal_vacuum(ctx: Context, args: list[str]) -> tuple[str, dict]:
    validate.no_arguments(args)
    ctx.emit.progress("cleanup", None, f"Reducing the system journal to {VACUUM_SIZE}B", cancellable=False)
    before = _journal_bytes(ctx)
    result = ctx.runner.run(argv("journalctl", f"--vacuum-size={VACUUM_SIZE}"), TIMEOUT)
    if result.code != 0:
        raise SysopsError("FAILED", f"Reducing the system journal failed (journalctl exit code {result.code}).",
                          {"detail": (result.stderr or result.stdout).strip()[-1000:]})
    freed = max(0, before - _journal_bytes(ctx))
    return f"System journal reduced ({freed} bytes freed).", {"freed_bytes": freed}
