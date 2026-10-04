"""boswas-winapp operations: install, inspect, upgrade, remove, list, launch,
stop, status, repair, logs, runtime.

Rules every operation follows:
  * Windows software never runs as root (install, upgrade, launch and repair
    refuse).
  * Windows code only ever runs inside the application's sandbox, through the
    AppArmor-attached runner.
  * What an application may access is recomputed from the catalog and the
    policy at every start, never read from state the application can change.
  * Boswas OS runs x86_64 (64-bit) Windows applications only; 32-bit
    software is refused before anything else is decided.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import UNSUPPORTED_32BIT_MESSAGE, envfile, paths, sandbox
from .catalog import Catalog
from .desktop import remove_entry, write_entry
from .errors import Busy, NotFound, Refused, Unavailable, UsageError, WinAppError
from .executor import KILL_GRACE_SECONDS, Executor, RunResult, sanitize
from .installer import InstallerInfo, copy_and_hash, hash_file, inspect, load_installer_types
from .manifest import Manifest, valid_id, windows_path_problem
from .policy import Grants, Policy, manifest_grants
from .store import AppStore, list_all_users, utc_now

ENFORCED_LABEL = f"{paths.APPARMOR_PROFILE} (enforce)"
UNINSTALLER_RE = re.compile(r"^(unins\d*|uninst(all)?.*|.*uninstall.*)\.exe$", re.IGNORECASE)
MAX_SNAPSHOT_ENTRIES = 200_000
STOP_WAIT_SECONDS = KILL_GRACE_SECONDS + 5

# Normalised application states (also the device agent's AppState values).
APP_STATES = ("INSTALLING", "INSTALLED", "RUNNING", "STOPPED", "ERROR", "REPAIR_REQUIRED", "BLOCKED",
              "UNSUPPORTED")
# Whether this runtime can run an application at all (catalog "compatibility").
SUPPORT_CODES = ("SUPPORTED", "UNSUPPORTED_ARCHITECTURE", "UNSUPPORTED_RUNTIME", "MISSING_DEPENDENCIES")


# --- context ---------------------------------------------------------------------

def load_runtime() -> sandbox.Runtime:
    values = envfile.load(paths.system_path(paths.RUNTIME_FILE))
    default = sandbox.Runtime()
    return sandbox.Runtime(
        loader=values.get("WINE_LOADER", default.loader),
        server=values.get("WINE_SERVER", default.server),
        wine_major=values.get("WINE_MAJOR", default.wine_major),
        architectures=tuple(values.get("ARCHITECTURES", " ".join(default.architectures)).split()),
        winearch=values.get("WINEARCH", default.winearch),
        winedebug=values.get("WINEDEBUG", default.winedebug),
        dll_overrides=values.get("DLL_OVERRIDES", default.dll_overrides),
    )


@dataclass
class Context:
    home: Path
    store: AppStore
    catalog: Catalog
    policy: Policy
    runtime: sandbox.Runtime
    session: sandbox.HostSession
    executor: Executor
    out: TextIO | None = None
    disable_userns: bool = True

    @classmethod
    def create(cls, out: TextIO | None = None) -> "Context":
        home = paths.user_home()
        return cls(home=home, store=AppStore(home), catalog=Catalog(), policy=Policy.load(),
                   runtime=load_runtime(), session=sandbox.HostSession.current(home),
                   executor=Executor(), out=out, disable_userns=_bwrap_has_disable_userns())

    def say(self, message: str) -> None:
        if self.out is not None:
            # Messages can contain names the application chose (program paths).
            self.out.write(f"boswas-winapp: {sanitize(message)}\n")
            self.out.flush()


def _bwrap_version() -> tuple[int, int] | None:
    try:
        out = subprocess.run([paths.BWRAP, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _bwrap_has_disable_userns() -> bool:
    version = _bwrap_version()
    return version is not None and version >= (0, 8)


def require_user() -> None:
    if os.geteuid() == 0:
        raise Refused("refusing to run Windows software as root; run boswas-winapp as the user "
                      "who will use the application", reason="root")
    if os.getuid() != os.geteuid():
        raise Refused("refusing to run with different real and effective user IDs", reason="setuid")


def check_runtime(ctx: Context) -> None:
    for path, what in ((ctx.runtime.loader, "Wine loader (package wine64)"),
                       (ctx.runtime.server, "wineserver (package wine64)"),
                       (paths.BWRAP, "bubblewrap"),
                       (paths.RUNNER, "Boswas runner (package boswas-compat)")):
        if not os.access(paths.system_path(path), os.X_OK):
            raise Unavailable(f"{what} is missing: {path}", reason="runtime-missing")


def wine_version(ctx: Context) -> str | None:
    try:
        out = subprocess.run([str(paths.system_path(ctx.runtime.loader)), "--version"], capture_output=True,
                             text=True, timeout=30, stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.splitlines()[0] if out else None


# --- architecture ------------------------------------------------------------------

def architecture_problem(ctx: Context, machine: str | None, what: str) -> str | None:
    """Why a Windows architecture cannot run here (None: it can, or it is unknown).

    32-bit software always gets the product's fixed sentence: Boswas OS v1 is
    64-bit only by decision, so no hint at a 32-bit runtime is ever given.
    """
    if machine is None or machine in ctx.runtime.architectures:
        return None
    supported = ", ".join(ctx.runtime.architectures)
    if machine == "x86":
        return (f"{UNSUPPORTED_32BIT_MESSAGE} ({what} is an x86 (32-bit) Windows program; Boswas OS runs "
                f"{supported} (64-bit) Windows applications only.)")
    return f"{what} is a {machine} Windows program; Boswas OS runs {supported} (64-bit) Windows applications only."


# --- paths inside drive_c ------------------------------------------------------------

def _drive_c(appdir: Path) -> Path:
    return appdir / "sandbox" / "prefix" / "drive_c"


def _windows_parts(winpath: str) -> list[str]:
    path = winpath.replace("/", "\\")
    if len(path) >= 2 and path[1] == ":":
        path = path[2:]
    return [p for p in path.split("\\") if p]


def _to_windows(parts: list[str]) -> str:
    return "C:\\" + "\\".join(parts)


def _real_dir(path: Path) -> bool:
    """A real directory, not a symbolic link (lstat)."""
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _safe_drive_c(appdir: Path) -> Path | None:
    """drive_c, if every component below the application directory is a real
    directory. The application controls the contents of sandbox/ and may
    replace any of them with symbolic links to the user's files; host-side
    code must never walk through those."""
    path = appdir
    for part in ("sandbox", "prefix", "drive_c"):
        path = path / part
        if not _real_dir(path):
            return None
    return path


def _prefix_initialised(appdir: Path) -> bool:
    prefix = appdir / "sandbox" / "prefix"
    if not _real_dir(prefix):
        return False
    try:
        return stat.S_ISREG((prefix / "system.reg").lstat().st_mode)
    except OSError:
        return False


def _safe_name(name: str) -> bool:
    """File names created by the application are shown to the user: no control characters."""
    return not any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in name)


def _resolve_ci(base: Path | None, parts: list[str]) -> list[str] | None:
    """Case-insensitive lookup below base; never follows symbolic links."""
    if base is None:
        return None
    current, actual = base, []
    for i, part in enumerate(parts):
        try:
            names = os.listdir(current)
        except OSError:
            return None
        match = part if part in names else next((n for n in names if n.lower() == part.lower()), None)
        if match is None:
            return None
        current = current / match
        actual.append(match)
        try:
            st = current.lstat()
        except OSError:
            return None
        last = i == len(parts) - 1
        if stat.S_ISLNK(st.st_mode) or (last and not stat.S_ISREG(st.st_mode)) or \
                (not last and not stat.S_ISDIR(st.st_mode)):
            return None
    return actual


def exe_snapshot(appdir: Path) -> set[str]:
    """Relative paths of .exe files in drive_c (outside C:\\windows)."""
    base = _safe_drive_c(appdir)
    found: set[str] = set()
    if base is None:
        return found
    seen = 0
    for root, dirs, files in os.walk(base, followlinks=False):
        rel_root = os.path.relpath(root, base)
        if rel_root == ".":
            dirs[:] = [d for d in dirs if d.lower() != "windows"]
        dirs[:] = [d for d in dirs if _safe_name(d)]
        for name in files:
            seen += 1
            if seen > MAX_SNAPSHOT_ENTRIES:
                return found
            if not _safe_name(name):
                continue
            if name.lower().endswith(".exe") and stat.S_ISREG(os.lstat(os.path.join(root, name)).st_mode):
                rel = name if rel_root == "." else os.path.join(rel_root, name)
                found.add(rel.replace(os.sep, "/"))
    return found


def find_launch_target(appdir: Path, launch: str) -> list[str] | None:
    """Resolve a manifest launch value to path components below drive_c."""
    if windows_path_problem(launch):
        return None
    parts = _windows_parts(launch)
    if len(parts) > 1:
        return _resolve_ci(_safe_drive_c(appdir), parts)
    matches = sorted(p for p in exe_snapshot(appdir) if p.rsplit("/", 1)[-1].lower() == parts[0].lower())
    return matches[0].split("/") if len(matches) == 1 else None


def _candidates(before: set[str], after: set[str]) -> list[str]:
    new = sorted(after - before)
    return [p for p in new if not UNINSTALLER_RE.match(p.rsplit("/", 1)[-1])]


# --- effective decision ----------------------------------------------------------------

@dataclass
class Effective:
    manifest: Manifest | None
    status: str
    source: str
    grants: Grants
    allowed: bool
    reason: str | None
    compatibility: str = "SUPPORTED"

    @property
    def supported(self) -> bool:
        return self.compatibility == "SUPPORTED"

    def to_dict(self) -> dict:
        return {"status": self.status, "manifest_source": self.source, "allowed": self.allowed,
                "reason": self.reason, "supported": self.supported, "compatibility": self.compatibility,
                "sandbox": self.grants.to_dict()}


def manifest_support(ctx: Context, manifest: Manifest) -> tuple[str, str | None, str | None]:
    """(support code, refusal reason, message) of a manifest on this runtime."""
    problem = architecture_problem(ctx, manifest.architecture, manifest.id)
    if problem:
        return "UNSUPPORTED_ARCHITECTURE", "architecture", problem
    if manifest.wine_version.split(".")[0] != ctx.runtime.wine_major:
        return ("UNSUPPORTED_RUNTIME", "wine-version",
                f"{manifest.id} requires Wine {manifest.wine_version}; this runtime is Wine {ctx.runtime.wine_major}")
    if manifest.dependencies:
        return ("MISSING_DEPENDENCIES", "dependencies",
                f"{manifest.id} needs runtime components ({', '.join(manifest.dependencies)}) that "
                "Boswas does not provide yet")
    if manifest.winetricks:
        denied = [v for v in manifest.winetricks if v not in ctx.policy.winetricks_allowed]
        if denied:
            return ("MISSING_DEPENDENCIES", "winetricks",
                    f"{manifest.id} requests winetricks verbs not allowed by policy: {', '.join(denied)}")
        return ("MISSING_DEPENDENCIES", "winetricks",
                f"{manifest.id} requests winetricks verbs; winetricks is not part of this release")
    return "SUPPORTED", None, None


def check_manifest_runtime(ctx: Context, manifest: Manifest) -> None:
    code, reason, message = manifest_support(ctx, manifest)
    if code != "SUPPORTED":
        raise Refused(message or code, reason=reason)


def effective(ctx: Context, app_id: str, installed_source: str, installer_sha256: str | None) -> Effective:
    """Status, sandbox grants and policy decision for an application, from the
    current catalog and policy (not from anything the application can write)."""
    blocked = [m for m in ctx.catalog.by_installer_sha256(installer_sha256 or "") if m.status == "blocked"]
    manifest = ctx.catalog.get(app_id)
    if manifest is not None:
        pin = manifest.installer.sha256
        if installed_source == "unlisted" and pin != installer_sha256:
            manifest = None          # same ID, different application: stays unlisted
        elif pin and pin != installer_sha256:
            return Effective(manifest, manifest.status, manifest.layer, manifest_grants(manifest), False,
                             "installed from a different installer than the catalog now specifies; reinstall it")
    if blocked:
        return Effective(manifest, "blocked", blocked[0].layer, Grants(), False,
                         "the installer is blocked by the Boswas compatibility catalog")
    status = manifest.status if manifest is not None else "unknown"
    source = manifest.layer if manifest is not None else "unlisted"
    grants = manifest_grants(manifest) if manifest is not None else ctx.policy.unlisted_grants()
    if manifest is not None:
        code, _reason, message = manifest_support(ctx, manifest)
        if code != "SUPPORTED":
            return Effective(manifest, status, source, grants, False, message, compatibility=code)
    try:
        ctx.policy.check_application(app_id)
    except Refused as exc:
        return Effective(manifest, status, source, grants, False, str(exc))
    if manifest is None:
        allowed, reason = ctx.policy.unlisted_apps, None
        if not allowed:
            reason = "policy does not allow unlisted applications (UNLISTED_APPS=deny)"
        elif installed_source != "unlisted":
            reason = f"manifest no longer in the catalog (was: {installed_source}); unlisted rules apply"
        return Effective(None, "unknown", "unlisted", grants, allowed, reason)
    try:
        ctx.policy.check_status(manifest.status, app_id)
        allowed, reason = True, None
    except Refused as exc:
        allowed, reason = False, str(exc)
    return Effective(manifest, manifest.status, manifest.layer, grants, allowed, reason)


# --- running Windows code ------------------------------------------------------------------

def run_in_sandbox(ctx: Context, app_id: str, appdir: Path, grants: Grants, command: list[str], log: Path, *,
                   title: str, cwd: str | None = None, env: dict | None = None, mirror: TextIO | None = None,
                   timeout: float | None = None, stoppable: bool = False) -> RunResult:
    argv = sandbox.build_command(app_id=app_id, appdir=appdir, session=ctx.session, grants=grants,
                                 runtime=ctx.runtime, command=command, cwd=cwd, extra_env=env,
                                 disable_userns=ctx.disable_userns,
                                 require_apparmor=ctx.policy.require_apparmor)
    kwargs = {"stoppable": True} if stoppable else {}
    result = ctx.executor.run(argv, log, mirror=mirror, timeout=timeout, title=title, **kwargs)
    if result.refused:
        raise Unavailable(f"Windows code was not started: {result.refused} (see {log})", reason="confinement")
    if result.confinement is None:
        raise Unavailable(f"the sandbox did not start (bubblewrap exit {result.exit_code}; see {log})",
                          reason="sandbox")
    if ctx.policy.require_apparmor and result.confinement != ENFORCED_LABEL:
        raise Unavailable(f"Windows code ran without the enforcing AppArmor profile ({result.confinement})",
                          reason="confinement")
    return result


def _create_prefix(ctx: Context, app_id: str, appdir: Path, grants: Grants, log: Path) -> None:
    loader = ctx.runtime.loader
    ctx.say("creating the Wine prefix (first run takes a while)")
    result = run_in_sandbox(ctx, app_id, appdir, grants, [loader, "wineboot", "--init"], log, title="wineboot --init")
    if result.exit_code != 0 or not _prefix_initialised(appdir):
        raise WinAppError(f"creating the Wine prefix failed (exit {result.exit_code}; see {log})",
                          reason="prefix-failed")
    defaults = "Z:" + paths.PREFIX_DEFAULTS.replace("/", "\\")
    result = run_in_sandbox(ctx, app_id, appdir, grants, [loader, "regedit", "/S", defaults], log,
                            title="prefix defaults")
    if result.exit_code != 0:
        raise WinAppError(f"applying the Boswas prefix defaults failed (see {log})", reason="prefix-failed")


# --- install plan (install, inspect, upgrade) --------------------------------------------

def derive_id(file_name: str) -> str:
    stem = file_name.rsplit(".", 1)[0].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")[:80] or "app"
    return f"local.{slug}"


@dataclass
class InstallPlan:
    app_id: str
    name: str
    version: str
    status: str
    source: str
    grants: Grants
    kind: str
    manifest: Manifest | None = None


def plan_install(ctx: Context, sha256: str, info: InstallerInfo, file_name: str, *, app_id: str | None = None,
                 name: str | None = None, portable: bool = False) -> InstallPlan:
    """Decide how a verified installer would be installed, or refuse it.

    Order: architecture, catalog (blocked, pin), device policy. Nothing is
    created or run here.
    """
    problem = architecture_problem(ctx, info.machine, file_name)
    if problem:
        raise Refused(problem, reason="architecture")
    matches = ctx.catalog.by_installer_sha256(sha256)
    if any(m.status == "blocked" for m in matches):
        raise Refused(f"this installer (sha256 {sha256[:16]}...) is blocked by the Boswas compatibility catalog",
                      reason="blocked")
    manifest = None
    if app_id:
        manifest = ctx.catalog.get(app_id)
        if manifest and manifest.installer.sha256 and manifest.installer.sha256 != sha256:
            raise Refused(f"the installer does not match the {app_id} manifest (SHA-256 differs)",
                          reason="installer-mismatch")
    elif len(matches) == 1:
        manifest = matches[0]
    elif len(matches) > 1:
        raise UsageError("several catalog manifests pin this installer; choose one with --id "
                         f"({', '.join(m.id for m in matches)})")

    if manifest is not None:
        ctx.policy.check_status(manifest.status, manifest.id)
        check_manifest_runtime(ctx, manifest)
        plan = InstallPlan(manifest.id, manifest.name, manifest.version, manifest.status, manifest.layer,
                           manifest_grants(manifest), manifest.installer.type or ("portable" if portable else info.kind),
                           manifest)
    else:
        ctx.policy.check_unlisted()
        plan = InstallPlan(app_id or derive_id(file_name), name or file_name.rsplit(".", 1)[0], "unknown",
                           "unknown", "unlisted", ctx.policy.unlisted_grants(), "portable" if portable else info.kind)
    ctx.policy.check_application(plan.app_id)
    if plan.kind in ("portable", "exe") and info.kind != "exe":
        raise Refused(f"{file_name} is not a Windows program, but the {plan.kind} installer type expects one",
                      reason="bad-installer")
    if plan.kind == "msi" and info.kind != "msi":
        raise Refused(f"{file_name} is not a Windows Installer package", reason="bad-installer")
    return plan


# --- install ---------------------------------------------------------------------------------

def install(ctx: Context, installer_path: str, *, app_id: str | None = None, name: str | None = None,
            portable: bool = False, interactive: bool = False, installer_args: list[str] | None = None,
            timeout: float | None = None, verbose: bool = False) -> dict:
    require_user()
    check_runtime(ctx)
    if app_id is not None and not valid_id(app_id):
        raise UsageError(f"invalid application ID {app_id!r}: use a lower-case reverse-DNS name, e.g. local.my-app")
    src = Path(installer_path)
    if not src.is_file():
        raise NotFound(f"installer not found: {installer_path}")

    staging = ctx.store.new_staging()
    try:
        staged = staging / "installer"
        ctx.say(f"verifying {src.name}")
        sha256, size = copy_and_hash(src, staged, ctx.policy.max_installer_bytes)
        info = inspect(staged)
        plan = plan_install(ctx, sha256, info, src.name, app_id=app_id, name=name, portable=portable)
        appdir = ctx.store.create(plan.app_id)
        with ctx.store.lock(plan.app_id):
            return _install_into(ctx, appdir, staged, src, sha256, size, info, plan, interactive, installer_args,
                                 timeout, verbose)
    finally:
        ctx.store.discard_staging(staging)


def inspect_installer(ctx: Context, installer_path: str, *, app_id: str | None = None, name: str | None = None,
                      portable: bool = False) -> dict:
    """What `install` would decide for an installer, without creating or running anything."""
    if app_id is not None and not valid_id(app_id):
        raise UsageError(f"invalid application ID {app_id!r}: use a lower-case reverse-DNS name, e.g. local.my-app")
    src = Path(installer_path)
    if not src.is_file():
        raise NotFound(f"installer not found: {installer_path}")
    sha256, size = hash_file(src, ctx.policy.max_installer_bytes)
    info = inspect(src)
    problem = architecture_problem(ctx, info.machine, src.name)
    result = {
        "installer": {"file": src.name, "sha256": sha256, "size": size, **info.to_dict()},
        "architecture": {"detected": info.machine, "supported": None if info.machine is None else problem is None,
                         "runtime": list(ctx.runtime.architectures), "message": problem},
        "application": None, "sandbox": None,
    }
    try:
        plan = plan_install(ctx, sha256, info, src.name, app_id=app_id, name=name, portable=portable)
    except (Refused, UsageError) as exc:
        result["decision"] = {"allowed": False, "reason": exc.reason, "message": str(exc)}
        return result
    installed = ctx.store.exists(plan.app_id)
    result["application"] = {"id": plan.app_id, "name": plan.name, "version": plan.version, "status": plan.status,
                             "manifest_source": plan.source, "kind": plan.kind, "already_installed": installed}
    result["sandbox"] = plan.grants.to_dict()
    result["decision"] = ({"allowed": False, "reason": "already-installed",
                           "message": f"{plan.app_id} is already installed (remove, repair or upgrade it)"}
                          if installed else {"allowed": True, "reason": None, "message": None})
    return result


def _installer_arguments(kind: str, info: InstallerInfo, manifest: Manifest | None, interactive: bool,
                         explicit: list[str] | None) -> list[str]:
    if explicit:
        return list(explicit)
    if interactive:
        return []
    if manifest and manifest.installer.silent_args:
        return list(manifest.installer.silent_args)
    if kind == "msi":
        return list(load_installer_types()[1])
    return list(info.framework_silent_args)


def _run_installer(ctx: Context, app_id: str, appdir: Path, staged: Path, src: Path, info: InstallerInfo,
                   plan: InstallPlan, interactive: bool, installer_args: list[str] | None, timeout: float | None,
                   mirror: TextIO | None, log: Path) -> list[str] | None:
    """Run the verified installer copy in the sandbox (or copy a portable
    program into the prefix). Returns the launch path of a portable program."""
    file_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", src.name)[:120] or "installer"
    target = appdir / "sandbox" / "installer" / file_name
    os.replace(staged, target)
    loader = ctx.runtime.loader
    if plan.kind == "portable":
        # Never write through symbolic links the prefix may contain.
        drive_c = _safe_drive_c(appdir)
        if drive_c is None:
            raise WinAppError("the prefix has an unexpected layout", reason="prefix-failed")
        dest_dir = drive_c
        for part in ("Program Files", app_id):
            dest_dir = dest_dir / part
            if not dest_dir.exists() and not dest_dir.is_symlink():
                dest_dir.mkdir(mode=0o755)
            if not _real_dir(dest_dir):
                raise WinAppError(f"unexpected file at {dest_dir}", reason="prefix-failed")
        dest = dest_dir / file_name
        if dest.is_symlink() or (dest.exists() and not stat.S_ISREG(dest.lstat().st_mode)):
            raise WinAppError(f"unexpected file at {dest}", reason="prefix-failed")
        dest.unlink(missing_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o755)
        with os.fdopen(fd, "wb") as dst, open(target, "rb") as src_fh:
            shutil.copyfileobj(src_fh, dst)
        ctx.say("portable application copied into the prefix")
        return ["Program Files", app_id, file_name]
    view_installer = f"{paths.view_dir(app_id)}/installer/{file_name}"
    args = _installer_arguments(plan.kind, info, plan.manifest, interactive, installer_args)
    if plan.kind == "msi":
        command = [loader, "msiexec", "/i", "Z:" + view_installer.replace("/", "\\"), *args]
    else:
        command = [loader, view_installer, *args]
    ctx.say(f"running the installer in its sandbox{' (' + ' '.join(args) + ')' if args else ''}")
    env = dict(plan.manifest.environment) if plan.manifest else {}
    result = run_in_sandbox(ctx, app_id, appdir, plan.grants, command, log, title="installer", env=env,
                            mirror=mirror, timeout=timeout)
    if result.timed_out:
        raise WinAppError(f"the installer did not finish within {timeout:.0f} s (see {log})", reason="timeout")
    if result.exit_code != 0:
        raise WinAppError(f"the installer exited with code {result.exit_code} (see {log})",
                          reason="installer-failed")
    return None


def _install_into(ctx, appdir, staged, src, sha256, size, info, plan, interactive, installer_args, timeout,
                  verbose) -> dict:
    app_id = plan.app_id
    manifest = plan.manifest
    record = {
        "id": app_id, "name": plan.name, "version": plan.version,
        "publisher": manifest.publisher if manifest else None,
        "status": plan.status, "state": "installing",
        "architecture": info.machine,
        "manifest": {"source": plan.source, "digest": manifest.digest if manifest else None},
        "installer": {"file": src.name, "sha256": sha256, "size": size, "kind": plan.kind, **info.to_dict()},
        "installed_at": utc_now(),
        "runtime": {"wine": wine_version(ctx), "winearch": ctx.runtime.winearch},
        "launch": None, "launch_candidates": [],
    }
    ctx.store.write_record(app_id, record)
    log = ctx.store.new_log(app_id, "install")
    mirror = ctx.out if verbose else None
    try:
        _create_prefix(ctx, app_id, appdir, plan.grants, log)
        before = exe_snapshot(appdir)
        launch_parts = _run_installer(ctx, app_id, appdir, staged, src, info, plan, interactive, installer_args,
                                      timeout, mirror, log)
        after = exe_snapshot(appdir)
        if manifest is not None:
            launch_parts = find_launch_target(appdir, manifest.launch)
            if launch_parts is None:
                raise WinAppError(f"the installer finished, but the manifest's program {manifest.launch!r} "
                                  f"does not exist in the prefix (see {log})", reason="launch-missing")
            record["launch_candidates"] = []
        elif launch_parts is None:
            candidates = _candidates(before, after)
            record["launch_candidates"] = [_to_windows(c.split("/")) for c in candidates]
            if len(candidates) == 1:
                launch_parts = candidates[0].split("/")
        record["launch"] = _to_windows(launch_parts) if launch_parts else None
        record["state"] = "installed"
    except BaseException as exc:
        record["state"] = "failed"
        record["error"] = str(exc) if isinstance(exc, WinAppError) else f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _clear_installer_dir(appdir)
        ctx.store.write_record(app_id, record)

    launcher = None
    if record["launch"]:
        launcher = str(write_entry(ctx.home, app_id, plan.name, plan.status))
        ctx.say(f"installed {app_id}; start it with: boswas-winapp launch {app_id}")
    else:
        ctx.say(f"installed {app_id}, but its program is ambiguous; start it with "
                f"'boswas-winapp launch {app_id} --exe <program>' (candidates: "
                f"{', '.join(record['launch_candidates']) or 'none found'})")
    return {"application": _public(record), "sandbox": plan.grants.to_dict(), "log": str(log),
            "desktop_entry": launcher}


def _clear_installer_dir(appdir: Path) -> None:
    """Remove the installer copy after installation.

    The installer has just run with write access to this directory, so it may
    have replaced it with a symbolic link (e.g. to the user's home). Never list
    or descend through it: check the directory itself with lstat, remove it as
    a whole (rmtree refuses a symlink and never follows links inside), then
    recreate it empty.
    """
    sandbox_dir = appdir / "sandbox"
    if not _real_dir(sandbox_dir):
        return
    installer_dir = sandbox_dir / "installer"
    try:
        st = installer_dir.lstat()
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(installer_dir, ignore_errors=True)
        else:
            installer_dir.unlink(missing_ok=True)       # symlink or file planted by the application
    try:
        installer_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass


def _public(record: dict) -> dict:
    keys = ("id", "name", "version", "publisher", "status", "state", "installed_at", "updated_at", "launch",
            "launch_candidates", "error")
    out = {k: record.get(k) for k in keys if k in record}
    out["manifest_source"] = (record.get("manifest") or {}).get("source")
    return out


def _architecture(record: dict) -> str | None:
    value = record.get("architecture") or (record.get("installer") or {}).get("machine")
    return value if isinstance(value, str) else None


# --- upgrade (a newer installer into the existing prefix) ----------------------------------------

def upgrade(ctx: Context, app_id: str, installer_path: str, *, interactive: bool = False,
            installer_args: list[str] | None = None, timeout: float | None = None, verbose: bool = False) -> dict:
    """Run a newer installer of an installed application in its existing
    prefix, keeping the application's data. Catalogued applications accept
    only the installer their manifest pins; the usual architecture, catalog
    and policy checks apply."""
    require_user()
    check_runtime(ctx)
    record, appdir = _load(ctx, app_id)
    if record.get("state") != "installed":
        raise Refused(f"{app_id} is not completely installed (state: {record.get('state')}); repair or reinstall it",
                      reason="not-installed")
    src = Path(installer_path)
    if not src.is_file():
        raise NotFound(f"installer not found: {installer_path}")
    staging = ctx.store.new_staging()
    try:
        staged = staging / "installer"
        ctx.say(f"verifying {src.name}")
        sha256, size = copy_and_hash(src, staged, ctx.policy.max_installer_bytes)
        info = inspect(staged)
        if sha256 == (record.get("installer") or {}).get("sha256"):
            raise Refused(f"this installer is the one {app_id} was installed from; nothing to upgrade",
                          reason="up-to-date")
        plan = plan_install(ctx, sha256, info, src.name, app_id=app_id, name=record.get("name"))
        if plan.app_id != app_id:
            raise Refused(f"this installer belongs to {plan.app_id}, not {app_id}", reason="installer-mismatch")
        if not _prefix_initialised(appdir):
            raise Refused(f"the prefix of {app_id} is damaged; run 'boswas-winapp repair {app_id}' first",
                          reason="repair-required")
        with ctx.store.lock(app_id):
            log = ctx.store.new_log(app_id, "install")
            previous = {"version": record.get("version"), "sha256": (record.get("installer") or {}).get("sha256")}
            mirror = ctx.out if verbose else None
            try:
                before = exe_snapshot(appdir)
                launch_parts = _run_installer(ctx, app_id, appdir, staged, src, info, plan, interactive,
                                              installer_args, timeout, mirror, log)
                after = exe_snapshot(appdir)
                if plan.manifest is not None:
                    launch_parts = find_launch_target(appdir, plan.manifest.launch)
                    if launch_parts is None:
                        raise WinAppError(f"the installer finished, but the manifest's program "
                                          f"{plan.manifest.launch!r} does not exist in the prefix (see {log})",
                                          reason="launch-missing")
                elif launch_parts is None:
                    current = record.get("launch")
                    launch_parts = _resolve_ci(_safe_drive_c(appdir), _windows_parts(current)) if current else None
                    if launch_parts is None:
                        candidates = _candidates(before, after)
                        launch_parts = candidates[0].split("/") if len(candidates) == 1 else None
                record.update({
                    "name": plan.name, "version": plan.version, "status": plan.status, "architecture": info.machine,
                    "publisher": plan.manifest.publisher if plan.manifest else record.get("publisher"),
                    "manifest": {"source": plan.source, "digest": plan.manifest.digest if plan.manifest else None},
                    "installer": {"file": src.name, "sha256": sha256, "size": size, "kind": plan.kind,
                                  **info.to_dict()},
                    "launch": _to_windows(launch_parts) if launch_parts else None,
                    "upgraded_at": utc_now(), "previous": previous, "state": "installed",
                })
                record.pop("error", None)
            except BaseException as exc:
                record["state"] = "failed"
                detail = str(exc) if isinstance(exc, WinAppError) else f"{type(exc).__name__}: {exc}"
                record["error"] = f"upgrade failed: {detail}"
                raise
            finally:
                _clear_installer_dir(appdir)
                ctx.store.write_record(app_id, record)
    finally:
        ctx.store.discard_staging(staging)
    if record.get("launch"):
        write_entry(ctx.home, app_id, plan.name, plan.status)
    ctx.say(f"upgraded {app_id} from {previous['version']} to {plan.version}")
    return {"application": _public(record), "previous": previous, "sandbox": plan.grants.to_dict(), "log": str(log)}


# --- other operations ---------------------------------------------------------------------------

def _load(ctx: Context, app_id: str) -> tuple[dict, Path]:
    if not valid_id(app_id):
        raise NotFound(f"invalid application ID {app_id!r}")
    appdir = ctx.store.check_layout(app_id)
    record = ctx.store.read_record(app_id)
    if record is None or record.get("id") != app_id:
        raise NotFound(f"{app_id} has no valid installation record (remove it and install again)")
    return record, appdir


def _effective_for(ctx: Context, record: dict) -> Effective:
    return effective(ctx, record["id"], (record.get("manifest") or {}).get("source", "unlisted"),
                     (record.get("installer") or {}).get("sha256"))


def _health(appdir: Path, record: dict) -> dict:
    launch = record.get("launch")
    program = None
    if launch:
        program = _resolve_ci(_safe_drive_c(appdir), _windows_parts(launch)) is not None
    return {"prefix": _prefix_initialised(appdir), "program": program}


def app_state(record: dict, eff: Effective, health: dict, running: bool, run: dict | None) -> tuple[str, str | None]:
    """Normalised state (APP_STATES) of an installed application and why."""
    if not eff.supported:
        return "UNSUPPORTED", eff.reason
    state = record.get("state")
    if running and (run or {}).get("operation") == "launch":
        return "RUNNING", None
    if state == "installing":
        return "INSTALLING", None
    if state != "installed":
        return "ERROR", record.get("error") or f"installation state: {state}"
    if not eff.allowed:
        return "BLOCKED", eff.reason
    if not health["prefix"]:
        return "REPAIR_REQUIRED", "the Wine prefix is missing or damaged"
    if health["program"] is False:
        return "REPAIR_REQUIRED", f"the program {record.get('launch')} is missing"
    if (record.get("last_launch") or {}).get("stopped"):
        return "STOPPED", None
    return "INSTALLED", None


def _last_launch_summary(record: dict) -> dict | None:
    last = record.get("last_launch")
    if not isinstance(last, dict):
        return None
    return {k: last.get(k) for k in ("at", "exit_code", "stopped", "timed_out", "confinement")}


def list_apps(ctx: Context, all_users: bool = False) -> dict:
    if all_users:
        users = list_all_users()
        for user in users:
            for app in user["applications"]:
                # Report the status that applies now (catalog and policy), not
                # the one stored in the user-writable record.
                if app.get("id") and valid_id(app["id"]):
                    eff = effective(ctx, app["id"], app.get("manifest_source") or "unlisted",
                                    app.get("installer_sha256"))
                    app.update(status=eff.status, allowed=eff.allowed, compatibility=eff.compatibility)
        return {"users": users}
    apps = []
    for app_id in ctx.store.list_ids():
        record = ctx.store.read_record(app_id)
        if not record or record.get("id") != app_id:
            apps.append({"id": app_id, "state": "damaged", "app_state": "ERROR",
                         "state_reason": "no valid installation record"})
            continue
        eff = _effective_for(ctx, record)
        appdir = ctx.store.app_dir(app_id)
        running = ctx.store.is_locked(app_id)
        state, why = app_state(record, eff, _health(appdir, record), running, ctx.store.read_run(app_id))
        item = _public(record)
        item.update(status=eff.status, allowed=eff.allowed, running=running, app_state=state, state_reason=why,
                    architecture=_architecture(record), supported=eff.supported, compatibility=eff.compatibility,
                    last_launch=_last_launch_summary(record))
        apps.append(item)
    return {"applications": apps}


def status(ctx: Context, app_id: str) -> dict:
    record, appdir = _load(ctx, app_id)
    eff = _effective_for(ctx, record)
    running = ctx.store.is_locked(app_id)
    health = _health(appdir, record)
    state, why = app_state(record, eff, health, running, ctx.store.read_run(app_id))
    return {
        "application": {**_public(record), "status": eff.status, "architecture": _architecture(record),
                        "app_state": state, "state_reason": why},
        "installer": record.get("installer"),
        "runtime": record.get("runtime"),
        "policy": {"allowed": eff.allowed, "reason": eff.reason, "supported": eff.supported,
                   "compatibility": eff.compatibility},
        "sandbox": eff.grants.to_dict(),
        "running": running,
        "health": health,
        "last_launch": record.get("last_launch"),
        "paths": {"data": str(appdir), "prefix": str(appdir / "sandbox" / "prefix"),
                  "sandbox_view": paths.view_dir(app_id), "logs": str(appdir / "logs")},
    }


def _launch_target(appdir: Path, record: dict, exe: str | None) -> list[str]:
    if exe:
        why = windows_path_problem(exe)
        if why:
            raise UsageError(f"--exe: {why}")
        parts = find_launch_target(appdir, exe)
        if parts is None:
            raise NotFound(f"program {exe!r} not found in the application's prefix")
        return parts
    if not record.get("launch"):
        raise UsageError("this application has no default program; choose one with --exe "
                         f"(candidates: {', '.join(record.get('launch_candidates') or []) or 'none'})")
    parts = _resolve_ci(_safe_drive_c(appdir), _windows_parts(record["launch"]))
    if parts is None:
        raise NotFound(f"the program {record['launch']} no longer exists; run 'boswas-winapp repair "
                       f"{record['id']}' or reinstall")
    return parts


def launch(ctx: Context, app_id: str, args: list[str], *, exe: str | None = None, timeout: float | None = None,
           mirror: TextIO | None = None) -> dict:
    require_user()
    check_runtime(ctx)
    record, appdir = _load(ctx, app_id)
    if record.get("state") != "installed":
        raise Refused(f"{app_id} is not completely installed (state: {record.get('state')})", reason="not-installed")
    eff = _effective_for(ctx, record)
    if not eff.allowed:
        reason = "architecture" if eff.compatibility == "UNSUPPORTED_ARCHITECTURE" else "policy"
        raise Refused(f"{app_id} may not be started: {eff.reason}", reason=reason)
    if eff.manifest is not None:
        check_manifest_runtime(ctx, eff.manifest)
    parts = _launch_target(appdir, record, exe)
    winpath = _to_windows(parts)
    cwd = f"{paths.view_dir(app_id)}/prefix/drive_c/" + "/".join(parts[:-1]) if len(parts) > 1 else None
    command = [ctx.runtime.loader, winpath, *(eff.manifest.arguments if eff.manifest else ()), *args]
    env = dict(eff.manifest.environment) if eff.manifest else {}
    with ctx.store.lock(app_id):
        ctx.store.write_run(app_id, {"operation": "launch", "pid": os.getpid(), "started_at": utc_now(),
                                     "program": winpath})
        try:
            log = ctx.store.new_log(app_id, "launch")
            result = run_in_sandbox(ctx, app_id, appdir, eff.grants, command, log, title=f"launch {winpath}",
                                    cwd=cwd, env=env, mirror=mirror, timeout=timeout, stoppable=True)
            record["last_launch"] = {"at": utc_now(), "program": winpath, "exit_code": result.exit_code,
                                     "confinement": result.confinement, "timed_out": result.timed_out,
                                     "stopped": result.stopped, "log": str(log)}
            ctx.store.write_record(app_id, record)
        finally:
            ctx.store.clear_run(app_id)
    return {"application": app_id, "program": winpath, "sandbox": eff.grants.to_dict(), **result.to_dict()}


def stop(ctx: Context, app_id: str, *, wait: float = STOP_WAIT_SECONDS) -> dict:
    """Stop a running application: its launcher ends the sandbox and every
    Windows process in it (like "End task"; unsaved work is lost)."""
    require_user()
    _load(ctx, app_id)
    if not ctx.store.is_locked(app_id):
        return {"application": app_id, "was_running": False, "stopped": False}
    run = ctx.store.read_run(app_id) or {}
    pid = run.get("pid")
    if run.get("operation") != "launch" or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise Busy(f"{app_id} is busy with another boswas-winapp operation (install, upgrade, repair or remove) "
                   "and cannot be stopped")
    # The process named in run.json must be the one holding the lock, and ours.
    if ctx.store.lock_holder(app_id) != pid:
        raise Refused(f"the running instance of {app_id} could not be identified safely; nothing was stopped",
                      reason="stop-unverified")
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            raise Refused(f"the running instance of {app_id} belongs to another user", reason="stop-unverified")
        os.kill(pid, signal.SIGTERM)
    except (FileNotFoundError, ProcessLookupError):
        pass                                     # it has just exited
    deadline = time.monotonic() + wait
    while ctx.store.is_locked(app_id) and time.monotonic() < deadline:
        time.sleep(0.2)
    if ctx.store.is_locked(app_id):
        raise WinAppError(f"{app_id} did not stop within {wait:.0f} s", reason="stop-timeout")
    ctx.say(f"stopped {app_id}")
    return {"application": app_id, "was_running": True, "stopped": True}


def remove(ctx: Context, app_id: str) -> dict:
    require_user()
    if not valid_id(app_id):
        raise NotFound(f"invalid application ID {app_id!r}")
    if not ctx.store.exists(app_id):
        raise NotFound(f"{app_id} is not installed")
    appdir = ctx.store.app_dir(app_id)
    if not appdir.is_symlink():
        with ctx.store.lock(app_id):
            pass                      # refuse (Busy) while it runs
    removed_entry = remove_entry(ctx.home, app_id)
    ctx.store.remove(app_id)
    ctx.say(f"removed {app_id} and its prefix")
    return {"application": app_id, "removed": True, "desktop_entry_removed": removed_entry}


def repair(ctx: Context, app_id: str, *, timeout: float | None = None) -> dict:
    require_user()
    check_runtime(ctx)
    record, appdir = _load(ctx, app_id)
    eff = _effective_for(ctx, record)
    if not eff.allowed:
        # Repair starts Wine in the prefix, which runs code the application
        # registered there; a blocked or disallowed application must not run.
        raise Refused(f"{app_id} may not be started, so it cannot be repaired: {eff.reason}", reason="policy")
    actions: list[str] = []
    with ctx.store.lock(app_id):
        for sub in ("sandbox/prefix", "sandbox/home", "sandbox/installer"):
            path = appdir / sub
            if path.is_symlink() or (path.exists() and not _real_dir(path)):
                path.unlink()                    # planted by the application
                actions.append(f"removed a symbolic link or file at {sub}")
            if not path.exists():
                path.mkdir(mode=0o700)
                actions.append(f"recreated {sub}")
        log = ctx.store.new_log(app_id, "repair")
        prefix_ok = _prefix_initialised(appdir)
        if prefix_ok:
            ctx.say("updating the Wine prefix")
            result = run_in_sandbox(ctx, app_id, appdir, eff.grants, [ctx.runtime.loader, "wineboot", "--update"],
                                    log, title="wineboot --update", timeout=timeout)
            actions.append(f"wineboot --update (exit {result.exit_code})")
            defaults = "Z:" + paths.PREFIX_DEFAULTS.replace("/", "\\")
            run_in_sandbox(ctx, app_id, appdir, eff.grants, [ctx.runtime.loader, "regedit", "/S", defaults], log,
                           title="prefix defaults", timeout=timeout)
            actions.append("re-applied Boswas prefix defaults")
        else:
            _create_prefix(ctx, app_id, appdir, eff.grants, log)
            actions.append("recreated the Wine prefix (the application must be reinstalled)")
        launch_ok = bool(record.get("launch")) and \
            _resolve_ci(_safe_drive_c(appdir), _windows_parts(record["launch"])) is not None
        if record.get("launch") and launch_ok:
            write_entry(ctx.home, app_id, record.get("name") or app_id, eff.status)
            actions.append("rewrote the desktop entry")
        record["repaired_at"] = utc_now()
        record["runtime"] = {"wine": wine_version(ctx), "winearch": ctx.runtime.winearch}
        if record.get("launch") and not launch_ok:
            record["state"] = "failed"
            record["error"] = f"program {record['launch']} missing after repair; reinstall the application"
        elif record.get("state") == "installed" or launch_ok:
            record["state"] = "installed"
            record.pop("error", None)
        ctx.store.write_record(app_id, record)
    healthy = record["state"] == "installed"
    ctx.say(f"repair {'completed' if healthy else 'finished; reinstall needed'}: {'; '.join(actions)}")
    return {"application": app_id, "healthy": healthy, "actions": actions, "log": str(log),
            "state": record["state"], "error": record.get("error")}


def logs(ctx: Context, app_id: str, *, kind: str | None = None, lines: int = 200) -> dict:
    _load(ctx, app_id)
    files = ctx.store.logs(app_id, kind)
    if not files:
        raise NotFound(f"no {kind + ' ' if kind else ''}logs for {app_id}")
    latest = files[-1]
    text = sanitize(latest.read_text(encoding="utf-8", errors="replace"))
    tail = text.splitlines()[-lines:] if lines > 0 else text.splitlines()
    return {"application": app_id, "log": str(latest), "available": [p.name for p in files], "lines": tail}


def clear_logs(ctx: Context, app_id: str) -> dict:
    """Delete an application's logs (refused while it runs or another operation holds it)."""
    require_user()
    _load(ctx, app_id)
    with ctx.store.lock(app_id):
        removed = ctx.store.clear_logs(app_id)
    ctx.say(f"removed {removed} log file(s) of {app_id}")
    return {"application": app_id, "removed": removed}


def catalog_list(ctx: Context) -> dict:
    manifests = []
    for m in ctx.catalog.all():
        code, _reason, message = manifest_support(ctx, m)
        manifests.append({**m.summary(), "supported": code == "SUPPORTED", "compatibility": code,
                          "support_message": message})
    return {"manifests": manifests, "problems": ctx.catalog.problems,
            "layers": [{"name": n, "path": str(p)} for n, p in ctx.catalog.layers],
            "policy": ctx.policy.to_dict()}


# --- runtime facts ------------------------------------------------------------------------------

def _apparmor_profile_mode() -> str:
    """Mode of the boswas-winapp profile: enforce, complain, not-loaded, unavailable or unknown."""
    enabled = paths.system_path("/sys/module/apparmor/parameters/enabled")
    try:
        if enabled.read_text().strip() != "Y":
            return "unavailable"
    except OSError:
        return "unavailable"
    try:
        text = paths.system_path("/sys/kernel/security/apparmor/profiles").read_text()
    except PermissionError:
        return "unknown"                         # only root may read the profile list
    except OSError:
        return "unknown"
    for line in text.splitlines():
        name, _, mode = line.rpartition(" ")
        if name == paths.APPARMOR_PROFILE:
            return mode.strip("()") or "unknown"
    return "not-loaded"


def runtime_info(ctx: Context) -> dict:
    """Health of the Windows compatibility runtime (read-only; any user)."""
    def available(path: str) -> bool:
        return os.access(paths.system_path(path), os.X_OK)

    wine_ok, server_ok = available(ctx.runtime.loader), available(ctx.runtime.server)
    bwrap_ok, runner_ok = available(paths.BWRAP), available(paths.RUNNER)
    bwrap = _bwrap_version() if bwrap_ok else None
    profile = _apparmor_profile_mode()
    confinement_ok = (not ctx.policy.require_apparmor) or profile in ("enforce", "unknown")
    return {
        "wine": {"available": wine_ok and server_ok, "version": wine_version(ctx) if wine_ok else None,
                 "major": ctx.runtime.wine_major, "loader": ctx.runtime.loader, "server": ctx.runtime.server},
        "architectures": list(ctx.runtime.architectures),
        "winearch": ctx.runtime.winearch,
        "bubblewrap": {"available": bwrap_ok, "version": ".".join(map(str, bwrap)) if bwrap else None,
                       "disable_userns": bool(bwrap and bwrap >= (0, 8))},
        "runner": {"available": runner_ok, "path": paths.RUNNER},
        "apparmor": {"profile": paths.APPARMOR_PROFILE, "mode": profile, "required": ctx.policy.require_apparmor},
        "policy": {"source": ctx.policy.source, "managed": ctx.policy.managed, "problems": list(ctx.policy.problems)},
        "healthy": wine_ok and server_ok and bwrap_ok and runner_ok and confinement_ok,
    }
