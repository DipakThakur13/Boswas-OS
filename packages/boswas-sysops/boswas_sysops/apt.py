"""APT operations: refresh, upgrade, install, remove, install a local .deb, clean.

Every change is simulated first (apt-get -s) and refused when it would
install a package excluded by Boswas policy or remove a protected one.
Then APT runs with its Status-Fd connected, so download and dpkg progress
become progress events; a cancel request stops APT only while it is
refreshing or downloading. APT's own signature verification is never
weakened (APT::Get::AllowUnauthenticated=false, no --allow-* and no dpkg
--force-* options except conffile handling).
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass, field

from . import locks, policy, validate
from .context import Context, Reporter
from .protocol import SysopsError
from .runner import STATUS_FD_TOKEN, argv

APT_OPTIONS = ("-q", "-o", "APT::Color=0", "-o", "Dpkg::Use-Pty=0",
               "-o", "APT::Get::AllowUnauthenticated=false",
               "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold")
STATUS_OPTION = ("-o", f"APT::Status-Fd={STATUS_FD_TOKEN}")
TIMEOUT_SIMULATE = 600.0
TIMEOUT_REFRESH = 3600.0
TIMEOUT_CHANGE = 6 * 3600.0
TIMEOUT_QUERY = 120.0
WORK_DIR = "/var/cache/boswas/sysops"
ARCHIVES = "/var/cache/apt/archives"
DEB_MAGIC = b"!<arch>\n"

HELD = re.compile(r"held by process (\d+) \(([^)]*)\)")
NOT_FOUND = re.compile(r"Unable to locate package (\S+)|Package '?([^' ]+)'? has no installation candidate")
SIM_LINE = re.compile(r"^(Inst|Remv|Purg) (\S+)(?: \[([^\]]*)\])?(?: \((\S+))?")
NETWORK_TEXT = ("Could not resolve", "Temporary failure resolving", "Failed to fetch", "Network is unreachable",
                "Connection timed out", "Could not connect to", "Unable to connect to", "No route to host")
UNTRUSTED_TEXT = ("NO_PUBKEY", "not signed", "couldn't be verified", "There were unauthenticated packages",
                  "cannot be authenticated", "EXPKEYSIG", "BADSIG", "invalid signature")
LOCK_TEXT = ("Could not get lock", "Unable to acquire the dpkg frontend lock", "Unable to lock the administration",
             "Unable to lock directory")
SPACE_TEXT = ("You don't have enough free space", "No space left on device")


# --- output analysis ---------------------------------------------------------------------------------

def classify(lines: list[str], what: str, code: int | None) -> SysopsError:
    """The SysopsError for a failed APT run, from its output."""
    text = "\n".join(lines)
    detail = {"detail": "\n".join(lines[-15:]), "exit_code": code}
    if any(t in text for t in LOCK_TEXT):
        match = HELD.search(text)
        holder = {"pid": int(match.group(1)), "command": match.group(2)} if match else None
        who = f" ({holder['command']} {holder['pid']})" if holder else ""
        return SysopsError("LOCKED", f"Another package manager is running{who}. Wait until it has finished, then "
                                     "try again.", {**detail, "holder": holder})
    if any(t in text for t in SPACE_TEXT):
        return SysopsError("DISK_SPACE", "There is not enough free disk space for this change. Free up space and "
                                         "try again.", detail)
    if any(t in text for t in UNTRUSTED_TEXT):
        return SysopsError("UNTRUSTED", "A package source could not be verified (missing or invalid signature). "
                                        "Nothing unverified was installed.", detail)
    match = NOT_FOUND.search(text)
    if match:
        name = match.group(1) or match.group(2)
        return SysopsError("NOT_FOUND", f"{name} is not available from the configured repositories.",
                           {**detail, "package": name})
    if any(t in text for t in NETWORK_TEXT):
        return SysopsError("NETWORK", "A download failed. Check the network connection and try again.", detail)
    return SysopsError("FAILED", f"{what} failed (apt-get exit code {code}).", detail)


@dataclass
class Plan:
    install: list[dict] = field(default_factory=list)    # {"package", "from", "to"}
    remove: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.install and not self.remove


def parse_simulation(output: str) -> Plan:
    plan = Plan()
    for line in output.splitlines():
        match = SIM_LINE.match(line)
        if not match:
            continue
        verb, package, old, new = match.groups()
        if verb == "Inst":
            plan.install.append({"package": package, "from": old, "to": new})
        else:
            plan.remove.append(package)
    return plan


class AptReporter(Reporter):
    """APT Status-Fd lines -> progress events."""

    def __init__(self, ctx: Context, download_phase: str = "download"):
        super().__init__(ctx)
        self.download_phase = download_phase
        self.errors: list[str] = []

    def status(self, line: str) -> None:
        kind, _, rest = line.partition(":")
        parts = rest.split(":", 2)
        if len(parts) < 3:
            return
        _item, percent_text, message = parts
        try:
            percent = float(percent_text)
        except ValueError:
            percent = None
        if kind == "dlstatus":
            if not self.changing:
                self.report(self.download_phase, percent, message)
        elif kind == "pmstatus":
            phase = pm_phase(message)
            if not self.changing:
                self.changing_begins(phase, percent, message)
            else:
                self.report(phase, percent, message)
        elif kind == "pmerror":
            self.errors.append(message)
            self.ctx.emit.log(f"dpkg: {message}", "warning")
        elif kind == "pmconffile":
            self.ctx.emit.log("A configuration file was kept in its current version.", "info")


def pm_phase(message: str) -> str:
    if message.startswith(("Removing", "Removed", "Preparing for removal", "Completely removing")):
        return "remove"
    if message.startswith(("Configuring", "Preparing to configure", "Installed", "Setting up", "Running")):
        return "configure"
    return "install"


# --- building blocks ---------------------------------------------------------------------------------

def simulate(ctx: Context, *args: str) -> Plan:
    command = argv("apt-get", *APT_OPTIONS, "-s", *args)
    result = ctx.runner.run(command, TIMEOUT_SIMULATE)
    lines = (result.stdout + "\n" + result.stderr).splitlines()
    if result.code != 0:
        raise classify(lines, "Checking the change", result.code)
    return parse_simulation(result.stdout)


def essential_or_required(ctx: Context, packages: list[str]) -> set[str]:
    if not packages:
        return set()
    names = sorted({validate.bare_name(p) for p in packages})
    # dpkg-query expands the \t and \n escape sequences of the format itself.
    result = ctx.runner.run(argv("dpkg-query", "-W", "-f", r"${Package}\t${Essential}\t${Priority}\n", *names),
                            TIMEOUT_QUERY)
    found = set()
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and (fields[1].strip() == "yes" or fields[2].strip() == "required"):
            found.add(fields[0])
    return found


def check_plan(ctx: Context, plan: Plan) -> None:
    excluded = sorted({validate.bare_name(i["package"]) for i in plan.install if policy.excluded(i["package"])})
    if excluded:
        raise SysopsError("NOT_ALLOWED", "This change would install software that Boswas OS policy keeps off this "
                                         f"device: {', '.join(excluded)}.", {"packages": excluded})
    protected = {validate.bare_name(p) for p in plan.remove if policy.protected_by_name(p)}
    protected |= essential_or_required(ctx, plan.remove)
    if protected:
        names = sorted(protected)
        raise SysopsError("NOT_ALLOWED", "This change would remove software Boswas OS needs: "
                                         f"{', '.join(names)}. Nothing was changed.", {"packages": names})


def run_apt(ctx: Context, what: str, *args: str, download_phase: str = "download",
            timeout: float = TIMEOUT_CHANGE, on_line=None) -> list[str]:
    """Run apt-get with progress; returns its output lines, raises SysopsError on failure or cancellation."""
    reporter = AptReporter(ctx, download_phase)
    command = argv("apt-get", *APT_OPTIONS, *STATUS_OPTION, *args)
    lines: list[str] = []

    def line(text: str) -> None:
        lines.append(text)
        if on_line is not None:
            on_line(text)

    result = ctx.runner.stream(command, timeout=timeout, on_line=line, on_status=reporter.status,
                               poll=reporter.poll)
    if result.cancelled:
        raise SysopsError("CANCELLED", "Cancelled. Nothing was changed.")
    if result.timed_out:
        raise SysopsError("FAILED", f"{what} did not finish within {int(timeout // 60)} minutes and was stopped.",
                          {"detail": result.detail})
    if result.code != 0:
        raise classify(lines or result.tail, what, result.code)
    return lines


def plan_items(plan: Plan) -> list[dict]:
    return [{"package": i["package"], "from": i["from"], "to": i["to"]} for i in plan.install]


# --- operations --------------------------------------------------------------------------------------

def refresh(ctx: Context, args: list[str]) -> tuple[str, dict]:
    validate.no_arguments(args)
    locks.check(ctx.root, locks.FOR_REFRESH)
    ctx.emit.progress("refresh", 0.0, "Refreshing package lists", cancellable=ctx.cancellable)
    lines = run_apt(ctx, "Refreshing the package lists", "update", download_phase="refresh",
                    timeout=TIMEOUT_REFRESH)
    errors, reached = refresh_errors(lines)
    for error in errors:
        ctx.emit.log(error, "warning")
    if errors and not reached:
        code = "UNTRUSTED" if any(t in " ".join(errors) for t in UNTRUSTED_TEXT) else "NETWORK"
        message = ("No package source could be verified." if code == "UNTRUSTED"
                   else "No package source could be reached. Check the network connection and try again.")
        raise SysopsError(code, message, {"errors": errors})
    if errors:
        return (f"Package lists refreshed; {len(errors)} source(s) could not be refreshed and keep their previous "
                "lists.", {"errors": errors})
    return "Package lists refreshed.", {"errors": []}


def refresh_errors(lines: list[str]) -> tuple[list[str], bool]:
    """(failed sources, whether at least one source answered) from apt-get update output."""
    errors: list[str] = []
    reached = False
    for line in lines:
        text = line.strip()
        if text.startswith(("Hit:", "Get:")):
            reached = True
        elif text.startswith("Err:"):
            entry = text.split(" ", 1)[1] if " " in text else text
            if entry not in errors:
                errors.append(entry)
        elif text.startswith(("W: ", "E: ")) and any(t in text for t in NETWORK_TEXT + UNTRUSTED_TEXT):
            entry = text[3:]
            if not entry.startswith("Some index files failed") and entry not in errors:
                errors.append(entry)
    return errors[:20], reached


def upgrade(ctx: Context, args: list[str]) -> tuple[str, dict]:
    validate.no_arguments(args)
    plan = simulate(ctx, "--with-new-pkgs", "upgrade")
    check_plan(ctx, plan)
    if plan.empty:
        return "Everything is up to date.", {"upgraded": []}
    locks.check(ctx.root, locks.FOR_PACKAGES)
    run_apt(ctx, "Installing updates", "-y", "--with-new-pkgs", "upgrade")
    items = plan_items(plan)
    return f"{len(items)} package(s) updated.", {"upgraded": items}


def install(ctx: Context, args: list[str]) -> tuple[str, dict]:
    names = validate.packages(args)
    refused = sorted({validate.bare_name(n) for n in names if policy.excluded(n)})
    if refused:
        raise SysopsError("NOT_ALLOWED", f"Boswas OS policy keeps this software off the device: {', '.join(refused)}.",
                          {"packages": refused})
    plan = simulate(ctx, "install", *names)
    check_plan(ctx, plan)
    if plan.empty:
        return "Already installed and up to date.", {"installed": []}
    locks.check(ctx.root, locks.FOR_PACKAGES)
    run_apt(ctx, "Installing", "-y", "install", *names)
    items = plan_items(plan)
    return f"Installed {', '.join(names)}.", {"installed": items}


def remove(ctx: Context, args: list[str]) -> tuple[str, dict]:
    names = validate.packages(args)
    plan = simulate(ctx, "remove", *names)
    check_plan(ctx, Plan(install=[], remove=plan.remove or []))
    if plan.install:
        raise SysopsError("NOT_ALLOWED", "Removing this would also install other packages; Boswas does not make "
                                         "that change automatically.", {"packages": plan_items(plan)})
    if not plan.remove:
        return "Not installed; nothing was removed.", {"removed": []}
    locks.check(ctx.root, locks.FOR_PACKAGES)
    run_apt(ctx, "Removing", "-y", "remove", *names)
    return f"Removed {', '.join(plan.remove)}.", {"removed": plan.remove}


def clean(ctx: Context, args: list[str]) -> tuple[str, dict]:
    validate.no_arguments(args)
    locks.check(ctx.root, locks.FOR_CLEAN)
    ctx.emit.progress("cleanup", None, "Removing downloaded package files", cancellable=False)
    before = ctx.root.tree_size(ARCHIVES)
    command = argv("apt-get", "-q", "clean")
    result = ctx.runner.run(command, TIMEOUT_QUERY)
    if result.code != 0:
        raise classify((result.stdout + "\n" + result.stderr).splitlines(), "Cleaning the package cache", result.code)
    freed = max(0, before - ctx.root.tree_size(ARCHIVES))
    return f"Removed downloaded package files ({freed} bytes).", {"freed_bytes": freed}


# --- local .deb -----------------------------------------------------------------------------------------

def _private_dir(ctx: Context) -> str:
    base = ctx.root(WORK_DIR)
    os.makedirs(base, mode=0o700, exist_ok=True)
    st = os.lstat(base)
    if not stat.S_ISDIR(st.st_mode) or (os.geteuid() == 0 and st.st_uid != 0):
        raise SysopsError("FAILED", f"{WORK_DIR} is not a directory owned by root.")
    os.chmod(base, 0o700)
    return tempfile.mkdtemp(prefix="local-deb-", dir=base)


def secure_copy(ctx: Context, path: str) -> tuple[str, str]:
    """Copy the user's .deb into a root-only directory; everything after this inspects the copy."""
    try:
        fd = os.open(ctx.root(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY)
    except FileNotFoundError:
        raise SysopsError("INVALID_ARGUMENT", f"{path} does not exist.") from None
    except OSError as exc:
        raise SysopsError("INVALID_ARGUMENT", f"{path} cannot be opened ({exc.strerror}).") from None
    workdir = None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise SysopsError("INVALID_ARGUMENT", f"{path} is not a regular file.")
        if st.st_uid not in (0, ctx.uid):
            raise SysopsError("NOT_ALLOWED", f"{path} belongs to another user.")
        if st.st_size > validate.MAX_LOCAL_DEB:
            raise SysopsError("INVALID_ARGUMENT", f"{path} is larger than 2 GB.")
        workdir = _private_dir(ctx)
        copy = os.path.join(workdir, "package.deb")
        out = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        copied = 0
        head = b""
        try:
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                if not head:
                    head = chunk[:len(DEB_MAGIC)]
                copied += len(chunk)
                if copied > validate.MAX_LOCAL_DEB:
                    raise SysopsError("INVALID_ARGUMENT", f"{path} is larger than 2 GB.")
                os.write(out, chunk)
        finally:
            os.close(out)
        if copied != st.st_size:
            raise SysopsError("INVALID_ARGUMENT", f"{path} changed while it was being read.")
        if head != DEB_MAGIC:
            raise SysopsError("INVALID_ARGUMENT", f"{path} is not a Debian package.")
        return workdir, copy
    except BaseException:
        if workdir is not None:
            _remove_tree(workdir)
        raise
    finally:
        os.close(fd)


def _remove_tree(path: str) -> None:
    for current, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.unlink(os.path.join(current, name))
        for name in dirs:
            os.rmdir(os.path.join(current, name))
    os.rmdir(path)


def install_local(ctx: Context, args: list[str]) -> tuple[str, dict]:
    path = validate.local_deb_path(args)
    policy.require_allowed(ctx.root, "LOCAL_DEB", "Installing packages from local files")
    workdir, copy = secure_copy(ctx, path)
    try:
        ctx.emit.progress("inspect", None, "Checking the package file", cancellable=ctx.cancellable)
        info = ctx.runner.run(argv("dpkg-deb", "--field", copy, "Package", "Version", "Architecture"), TIMEOUT_QUERY)
        fields = dict(line.split(": ", 1) for line in info.stdout.splitlines() if ": " in line)
        package, version, arch = fields.get("Package", ""), fields.get("Version", ""), fields.get("Architecture", "")
        if info.code != 0 or not validate.PACKAGE.fullmatch(package) or not version:
            raise SysopsError("INVALID_ARGUMENT", f"{path} is not a valid Debian package.",
                              {"detail": info.stderr.strip()[-500:]})
        if policy.excluded(package) or policy.protected_by_name(package):
            raise SysopsError("NOT_ALLOWED", f"A local file may not install or replace {package}.",
                              {"package": package})
        plan = simulate(ctx, "install", copy)
        check_plan(ctx, plan)
        locks.check(ctx.root, locks.FOR_PACKAGES)
        run_apt(ctx, "Installing the package file", "-y", "install", copy)
    finally:
        _remove_tree(workdir)
    data = {"package": package, "version": version, "architecture": arch, "verified": False,
            "installed": plan_items(plan)}
    return (f"Installed {package} {version} from a local file. Its origin was not verified by a signed repository.",
            data)
