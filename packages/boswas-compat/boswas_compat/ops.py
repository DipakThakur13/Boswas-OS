"""boswas-winapp operations: install, remove, list, launch, status, repair, logs.

Rules every operation follows:
  * Windows software never runs as root (install, launch and repair refuse).
  * Windows code only ever runs inside the application's sandbox, through the
    AppArmor-attached runner.
  * What an application may access is recomputed from the catalog and the
    policy at every start, never read from state the application can change.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import envfile, paths, sandbox
from .catalog import Catalog
from .desktop import remove_entry, write_entry
from .errors import NotFound, Refused, Unavailable, UsageError, WinAppError
from .executor import Executor, RunResult, sanitize
from .installer import InstallerInfo, copy_and_hash, inspect, load_installer_types
from .manifest import Manifest, valid_id, windows_path_problem
from .policy import Grants, Policy, manifest_grants
from .store import AppStore, list_all_users, utc_now

ENFORCED_LABEL = f"{paths.APPARMOR_PROFILE} (enforce)"
UNINSTALLER_RE = re.compile(r"^(unins\d*|uninst(all)?.*|.*uninstall.*)\.exe$", re.IGNORECASE)
MAX_SNAPSHOT_ENTRIES = 200_000


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


def _bwrap_has_disable_userns() -> bool:
    try:
        out = subprocess.run([paths.BWRAP, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    m = re.search(r"(\d+)\.(\d+)", out)
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (0, 8)


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

    def to_dict(self) -> dict:
        return {"status": self.status, "manifest_source": self.source, "allowed": self.allowed,
                "reason": self.reason, "sandbox": self.grants.to_dict()}


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
    if manifest is None:
        grants = ctx.policy.unlisted_grants()
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
    return Effective(manifest, manifest.status, manifest.layer, manifest_grants(manifest), allowed, reason)


def check_manifest_runtime(ctx: Context, manifest: Manifest) -> None:
    if manifest.architecture not in ctx.runtime.architectures:
        raise Refused(f"{manifest.id} is a {manifest.architecture} application; this Wine runtime runs "
                      f"{', '.join(ctx.runtime.architectures)} only", reason="architecture")
    if manifest.wine_version.split(".")[0] != ctx.runtime.wine_major:
        raise Refused(f"{manifest.id} requires Wine {manifest.wine_version}; this runtime is Wine "
                      f"{ctx.runtime.wine_major}", reason="wine-version")
    if manifest.dependencies:
        raise Refused(f"{manifest.id} needs runtime components ({', '.join(manifest.dependencies)}) that "
                      "Boswas does not provide yet", reason="dependencies")
    if manifest.winetricks:
        denied = [v for v in manifest.winetricks if v not in ctx.policy.winetricks_allowed]
        if denied:
            raise Refused(f"{manifest.id} requests winetricks verbs not allowed by policy: {', '.join(denied)}",
                          reason="winetricks")
        raise Refused(f"{manifest.id} requests winetricks verbs; winetricks is not part of this release",
                      reason="winetricks")


# --- running Windows code ------------------------------------------------------------------

def run_in_sandbox(ctx: Context, app_id: str, appdir: Path, grants: Grants, command: list[str], log: Path, *,
                   title: str, cwd: str | None = None, env: dict | None = None, mirror: TextIO | None = None,
                   timeout: float | None = None) -> RunResult:
    argv = sandbox.build_command(app_id=app_id, appdir=appdir, session=ctx.session, grants=grants,
                                 runtime=ctx.runtime, command=command, cwd=cwd, extra_env=env,
                                 disable_userns=ctx.disable_userns,
                                 require_apparmor=ctx.policy.require_apparmor)
    result = ctx.executor.run(argv, log, mirror=mirror, timeout=timeout, title=title)
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


# --- install ---------------------------------------------------------------------------------

def derive_id(file_name: str) -> str:
    stem = file_name.rsplit(".", 1)[0].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")[:80] or "app"
    return f"local.{slug}"


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

        # Catalog: a blocked installer is refused whatever ID is requested.
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
            app_id, app_name, version, status, source = (manifest.id, manifest.name, manifest.version,
                                                          manifest.status, manifest.layer)
            grants = manifest_grants(manifest)
            kind = manifest.installer.type or ("portable" if portable else info.kind)
        else:
            ctx.policy.check_unlisted()
            app_id = app_id or derive_id(src.name)
            app_name = name or src.name.rsplit(".", 1)[0]
            version, status, source = "unknown", "unknown", "unlisted"
            grants = ctx.policy.unlisted_grants()
            kind = "portable" if portable else info.kind
        if kind in ("portable", "exe") and info.kind != "exe":
            raise Refused(f"{src.name} is not a Windows program, but the {kind} installer type expects one",
                          reason="bad-installer")
        if kind == "msi" and info.kind != "msi":
            raise Refused(f"{src.name} is not a Windows Installer package", reason="bad-installer")
        if info.kind == "exe" and info.machine not in ctx.runtime.architectures:
            raise Refused(f"{src.name} is a {info.machine} Windows program; this runtime runs "
                          f"{', '.join(ctx.runtime.architectures)} programs only (no 32-bit Wine in this "
                          "release; see docs/compatibility/README.md)", reason="architecture")

        appdir = ctx.store.create(app_id)
        with ctx.store.lock(app_id):
            return _install_into(ctx, appdir, staged, src, sha256, size, info, manifest, app_id, app_name,
                                 version, status, source, grants, kind, interactive, installer_args, timeout,
                                 verbose)
    finally:
        ctx.store.discard_staging(staging)


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


def _install_into(ctx, appdir, staged, src, sha256, size, info, manifest, app_id, app_name, version, status,
                  source, grants, kind, interactive, installer_args, timeout, verbose) -> dict:
    file_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", src.name)[:120] or "installer"
    record = {
        "id": app_id, "name": app_name, "version": version,
        "publisher": manifest.publisher if manifest else None,
        "status": status, "state": "installing",
        "manifest": {"source": source, "digest": manifest.digest if manifest else None},
        "installer": {"file": src.name, "sha256": sha256, "size": size, "kind": kind, **info.to_dict()},
        "installed_at": utc_now(),
        "runtime": {"wine": wine_version(ctx), "winearch": ctx.runtime.winearch},
        "launch": None, "launch_candidates": [],
    }
    ctx.store.write_record(app_id, record)
    log = ctx.store.new_log(app_id, "install")
    mirror = ctx.out if verbose else None
    env = dict(manifest.environment) if manifest else {}
    try:
        installer_dir = appdir / "sandbox" / "installer"
        target = installer_dir / file_name
        os.replace(staged, target)
        _create_prefix(ctx, app_id, appdir, grants, log)
        before = exe_snapshot(appdir)
        loader = ctx.runtime.loader
        if kind == "portable":
            # Only Wine's own wineboot has run in this fresh prefix; still never
            # write through symbolic links.
            drive_c = _safe_drive_c(appdir)
            if drive_c is None:
                raise WinAppError("the new prefix has an unexpected layout", reason="prefix-failed")
            dest_dir = drive_c
            for part in ("Program Files", app_id):
                dest_dir = dest_dir / part
                if not dest_dir.exists() and not dest_dir.is_symlink():
                    dest_dir.mkdir(mode=0o755)
                if not _real_dir(dest_dir):
                    raise WinAppError(f"unexpected file at {dest_dir}", reason="prefix-failed")
            fd = os.open(dest_dir / file_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o755)
            with os.fdopen(fd, "wb") as dst, open(target, "rb") as src_fh:
                shutil.copyfileobj(src_fh, dst)
            launch_parts: list[str] | None = ["Program Files", app_id, file_name]
            ctx.say("portable application copied into the prefix")
        else:
            view_installer = f"{paths.view_dir(app_id)}/installer/{file_name}"
            args = _installer_arguments(kind, info, manifest, interactive, installer_args)
            if kind == "msi":
                command = [loader, "msiexec", "/i", "Z:" + view_installer.replace("/", "\\"), *args]
            else:
                command = [loader, view_installer, *args]
            ctx.say(f"running the installer in its sandbox{' (' + ' '.join(args) + ')' if args else ''}")
            result = run_in_sandbox(ctx, app_id, appdir, grants, command, log, title="installer", env=env,
                                    mirror=mirror, timeout=timeout)
            if result.timed_out:
                raise WinAppError(f"the installer did not finish within {timeout:.0f} s (see {log})",
                                  reason="timeout")
            if result.exit_code != 0:
                raise WinAppError(f"the installer exited with code {result.exit_code} (see {log})",
                                  reason="installer-failed")
            launch_parts = None
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
        launcher = str(write_entry(ctx.home, app_id, app_name, status))
        ctx.say(f"installed {app_id}; start it with: boswas-winapp launch {app_id}")
    else:
        ctx.say(f"installed {app_id}, but its program is ambiguous; start it with "
                f"'boswas-winapp launch {app_id} --exe <program>' (candidates: "
                f"{', '.join(record['launch_candidates']) or 'none found'})")
    return {"application": _public(record), "sandbox": grants.to_dict(), "log": str(log),
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
                    app.update(status=eff.status, allowed=eff.allowed)
        return {"users": users}
    apps = []
    for app_id in ctx.store.list_ids():
        record = ctx.store.read_record(app_id)
        if not record or record.get("id") != app_id:
            apps.append({"id": app_id, "state": "damaged"})
            continue
        eff = _effective_for(ctx, record)
        item = _public(record)
        item.update(status=eff.status, allowed=eff.allowed, running=ctx.store.is_locked(app_id))
        apps.append(item)
    return {"applications": apps}


def status(ctx: Context, app_id: str) -> dict:
    record, appdir = _load(ctx, app_id)
    eff = _effective_for(ctx, record)
    return {
        "application": {**_public(record), "status": eff.status},
        "installer": record.get("installer"),
        "runtime": record.get("runtime"),
        "policy": {"allowed": eff.allowed, "reason": eff.reason},
        "sandbox": eff.grants.to_dict(),
        "running": ctx.store.is_locked(app_id),
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
        raise Refused(f"{app_id} may not be started: {eff.reason}", reason="policy")
    if eff.manifest is not None:
        check_manifest_runtime(ctx, eff.manifest)
    parts = _launch_target(appdir, record, exe)
    winpath = _to_windows(parts)
    cwd = f"{paths.view_dir(app_id)}/prefix/drive_c/" + "/".join(parts[:-1]) if len(parts) > 1 else None
    command = [ctx.runtime.loader, winpath, *(eff.manifest.arguments if eff.manifest else ()), *args]
    env = dict(eff.manifest.environment) if eff.manifest else {}
    with ctx.store.lock(app_id):
        log = ctx.store.new_log(app_id, "launch")
        result = run_in_sandbox(ctx, app_id, appdir, eff.grants, command, log, title=f"launch {winpath}",
                                cwd=cwd, env=env, mirror=mirror, timeout=timeout)
        record["last_launch"] = {"at": utc_now(), "program": winpath, "exit_code": result.exit_code,
                                 "confinement": result.confinement, "timed_out": result.timed_out,
                                 "log": str(log)}
        ctx.store.write_record(app_id, record)
    return {"application": app_id, "program": winpath, "sandbox": eff.grants.to_dict(), **result.to_dict()}


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


def catalog_list(ctx: Context) -> dict:
    return {"manifests": [m.summary() for m in ctx.catalog.all()], "problems": ctx.catalog.problems,
            "layers": [{"name": n, "path": str(p)} for n, p in ctx.catalog.layers],
            "policy": ctx.policy.to_dict()}
