"""firmware-report: firmware files the kernel asked for and did not find (read-only).

Source: the kernel messages of the current boot (journalctl -k -b 0, or
dmesg when there is no journal). Two message forms exist:

  r8169 0000:03:00.0: firmware: failed to load rtl_nic/rtl8125b-2.fw (-2)
  iwlwifi 0000:00:14.3: Direct firmware load for iwlwifi-so-a0-gf-a0-89.ucode failed with error -2

Each file is mapped to the Debian package that ships it: first the image's
index (/usr/share/boswas/firmware/index.tsv, generated at build time from the
Debian Contents indices), then dpkg for files present on disk. Installed
state and candidate version come from apt-cache policy. Nothing is changed;
installing a package is a separate, authorised apt-install.
"""

from __future__ import annotations

import re

from . import validate
from .context import Context
from .runner import argv

INDEX = "/usr/share/boswas/firmware/index.tsv"
FIRMWARE_DIRS = ("/usr/lib/firmware", "/lib/firmware")
TIMEOUT = 60.0
MESSAGE = re.compile(r"^(?P<driver>\S+?):? (?P<device>\S+?): (?:firmware: failed to load (?P<a>\S+)"
                     r"|Direct firmware load for (?P<b>\S+) failed)")
TIMESTAMP = re.compile(r"^\[\s*\d+(?:\.\d+)?\]\s*")
# Requested by drivers but optional (hw-detect ignores them too): debug-only firmware.
IGNORED = frozenset({"iwl-debug-yoyo.bin"})


def parse_kernel_log(text: str) -> list[dict]:
    missing: dict[str, dict] = {}
    for raw in text.splitlines():
        line = TIMESTAMP.sub("", raw.strip())
        match = MESSAGE.match(line)
        if not match:
            continue
        name = match.group("a") or match.group("b")
        if name in IGNORED or name in missing or not validate.firmware_file(name):
            continue
        missing[name] = {"file": name, "driver": match.group("driver"), "device": match.group("device")}
    return list(missing.values())


def load_index(root) -> dict[str, str]:
    index: dict[str, str] = {}
    for line in (root.read(INDEX, 16 * 1024 * 1024) or "").splitlines():
        path, _, package = line.partition("\t")
        path, package = path.strip(), package.strip()
        if validate.firmware_file(path) and validate.PACKAGE.fullmatch(package):
            index.setdefault(path, package)
    return index


def kernel_log(ctx: Context) -> tuple[str, str]:
    result = ctx.runner.run(argv("journalctl", "-k", "-b", "0", "-o", "cat", "--no-pager", "-q"), TIMEOUT)
    if result.code == 0:
        return result.stdout, "journal"
    result = ctx.runner.run(argv("dmesg", "--notime"), TIMEOUT)
    if result.code == 0:
        return result.stdout, "dmesg"
    return "", "none"


def owner_of(ctx: Context, name: str) -> str | None:
    for base in FIRMWARE_DIRS:
        path = f"{base}/{name}"
        if not ctx.root.exists(path):
            continue
        result = ctx.runner.run(argv("dpkg-query", "-S", path), TIMEOUT)
        if result.code == 0 and ":" in result.stdout:
            package = result.stdout.split(":", 1)[0].split(",")[0].strip()
            if validate.PACKAGE.fullmatch(package):
                return package
    return None


def policy_states(ctx: Context, packages: list[str]) -> dict[str, dict]:
    """{package: {"installed": bool, "candidate": str | None}} from apt-cache policy."""
    states: dict[str, dict] = {}
    if not packages:
        return states
    result = ctx.runner.run(argv("apt-cache", "policy", *sorted(set(packages))), TIMEOUT)
    current = None
    for line in result.stdout.splitlines():
        if line and not line.startswith(" ") and line.endswith(":"):
            current = line[:-1]
            states[current] = {"installed": False, "candidate": None}
        elif current is not None:
            text = line.strip()
            if text.startswith("Installed:"):
                states[current]["installed"] = text.split(":", 1)[1].strip() not in ("", "(none)")
            elif text.startswith("Candidate:"):
                value = text.split(":", 1)[1].strip()
                states[current]["candidate"] = None if value in ("", "(none)") else value
    return states


def report(ctx: Context, args: list[str]) -> tuple[str, dict]:
    validate.no_arguments(args)
    ctx.emit.progress("inspect", None, "Reading the kernel messages of this boot", cancellable=False)
    text, source = kernel_log(ctx)
    missing = parse_kernel_log(text)
    index = load_index(ctx.root) if missing else {}
    for entry in missing:
        entry["present"] = any(ctx.root.exists(f"{base}/{entry['file']}") for base in FIRMWARE_DIRS)
        entry["package"] = index.get(entry["file"]) or (owner_of(ctx, entry["file"]) if entry["present"] else None)
    states = policy_states(ctx, [e["package"] for e in missing if e["package"]])
    for entry in missing:
        state = states.get(entry["package"] or "", {})
        entry["installed"] = bool(state.get("installed")) if entry["package"] else entry["present"]
        entry["candidate"] = state.get("candidate")
    if source == "none":
        return "The kernel messages could not be read; missing firmware cannot be reported.", \
            {"source": source, "missing": []}
    if not missing:
        return "No missing firmware was reported since this computer started.", {"source": source, "missing": []}
    return (f"{len(missing)} firmware file(s) were missing when the hardware started.",
            {"source": source, "missing": missing})
