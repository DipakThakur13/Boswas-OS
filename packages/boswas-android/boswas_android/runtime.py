"""The Android runtime as Boswas OS sees it: status, session, apps, F-Droid, cleanup.

User-level steps run Waydroid's own commands as the desktop user through the
allowlisted runner; privileged steps go through boswas-sysops (sysops.py).
Nothing here starts Android or an app unless a command explicitly asks for it.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

from . import JSON_SCHEMA, apk, checks, cleanup, config, fdroid, googleplay, sysops, system, userlog
from . import runner as runner_mod
from . import waydroid as wd
from .errors import Failed, InvalidArgument, NotInitialized, NotRunning, Refused, RuntimeMissing, Unsupported
from .system import Host, Root

WAYDROID_BIN = "/usr/bin/waydroid"
DPKG_STATUS = "/var/lib/dpkg/status"
REPO_SOURCE = "/etc/apt/sources.list.d/boswas-waydroid.sources"
SERVICE = "waydroid-container.service"
BRIDGE = "waydroid0"
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
STATUS_TIMEOUT = 30.0
APP_TIMEOUT = 180.0
SESSION_START_WAIT = 120.0
INSTALL_CONFIRM_WAIT = 90.0
REMOVE_CONFIRM_WAIT = 45.0
POLL = 2.0


@dataclass
class Context:
    root: Root = field(default_factory=Root)
    host: Host = field(default_factory=Host)
    runner: object = None
    sleep: object = time.sleep
    monotonic: object = time.monotonic
    opener: object = None              # F-Droid download (tests inject a fake)

    def __post_init__(self):
        if self.runner is None:
            self.runner = runner_mod.Runner(self.host.environ)

    @property
    def conf(self) -> config.RuntimeConf:
        return config.runtime_conf(self.root)


def valid_package(name: str) -> str:
    if not isinstance(name, str) or len(name) > 255 or not PACKAGE_RE.match(name):
        raise InvalidArgument(f"{name!r} is not a valid Android package name.")
    return name


# --- facts ------------------------------------------------------------------------------------------

def runtime_installed(ctx: Context) -> tuple[bool, str | None]:
    version = wd.dpkg_version(ctx.root.read(DPKG_STATUS, 64 * 1024 * 1024), "waydroid")
    return (version is not None and ctx.root.isfile(WAYDROID_BIN)), version


def initialized(ctx: Context) -> bool:
    return ctx.root.isfile(config.WAYDROID_CFG) and ctx.root.isdir(config.WAYDROID_ROOTFS)


def _waydroid(ctx: Context, *args: str, timeout: float = STATUS_TIMEOUT):
    return ctx.runner.run(runner_mod.argv("waydroid", *args), timeout)


def session_state(ctx: Context) -> dict:
    """{"running", "state", "ip"} from `waydroid status` (stopped if it cannot be asked)."""
    try:
        result = _waydroid(ctx, "status")
    except runner_mod.RunnerError:
        return {"running": False, "state": None, "ip": None}
    parsed = wd.parse_status(result.stdout + "\n" + result.stderr)
    return {"running": parsed["running"], "state": parsed["state"], "ip": parsed["ip"]}


def service_active(ctx: Context) -> bool | None:
    try:
        result = ctx.runner.run(runner_mod.argv("systemctl", "is-active", SERVICE), 15.0)
    except runner_mod.RunnerError:
        return None
    word = result.stdout.strip()
    if word == "active":
        return True
    if word in ("inactive", "failed", "activating", "deactivating", "unknown"):
        return False
    return None


def network(ctx: Context) -> dict:
    present = ctx.root.exists(f"/sys/class/net/{BRIDGE}")
    address = None
    if present:
        try:
            result = ctx.runner.run(runner_mod.argv("ip", "-j", "-4", "addr", "show", "dev", BRIDGE), 10.0)
            address = wd.bridge_address(result.stdout) if result.code == 0 else None
        except runner_mod.RunnerError:
            address = None
    return {"interface": BRIDGE, "present": present, "address": address}


def status(ctx: Context) -> dict:
    installed, version = runtime_installed(ctx)
    cfg = config.waydroid_cfg(ctx.root)
    is_init = installed and initialized(ctx)
    images_path = (cfg or {}).get("images_path") or config.DEFAULT_IMAGES
    sizes = [ctx.root.size(f"{images_path}/{name}") for name in ("system.img", "vendor.img")]
    images_bytes = sum(s for s in sizes if s is not None) if any(s is not None for s in sizes) else None
    data_home = ctx.host.data_home()
    user_dir = os.path.join(data_home, "waydroid") if data_home else None
    user_bytes = (0 if not os.path.lexists(user_dir) else system.tree_bytes(user_dir)) if user_dir else None
    session = session_state(ctx) if is_init else {"running": False, "state": None, "ip": None}
    return {
        "schema": JSON_SCHEMA,
        "runtime": {"installed": installed, "version": version, "repository": ctx.root.isfile(REPO_SOURCE)},
        "compatibility": checks.compatibility(ctx.root, ctx.host, cfg, is_init),
        "initialized": is_init,
        "images": {"type": config.image_type_from_ota((cfg or {}).get("system_ota")) if is_init else None,
                   "system": wd.image_version((cfg or {}).get("system_datetime")) if is_init else None,
                   "vendor": wd.image_version((cfg or {}).get("vendor_datetime")) if is_init else None,
                   "bytes": images_bytes if is_init else None},
        "service": {"active": service_active(ctx) if installed else False},
        "session": {"running": session["running"], "state": session["state"]},
        "storage": {"images_bytes": images_bytes if is_init else None, "user_bytes": user_bytes},
        "network": network(ctx) if installed else {"interface": BRIDGE, "present": False, "address": None},
        "gpu": checks.gpu(ctx.root),
        "google_play": googleplay.assessment(),
    }


def runtime_abis(ctx: Context) -> tuple[str, ...]:
    return checks.runtime_abis(ctx.root, config.waydroid_cfg(ctx.root))


# --- preconditions ----------------------------------------------------------------------------------

def require_installed(ctx: Context) -> None:
    if not runtime_installed(ctx)[0]:
        raise RuntimeMissing("The Android runtime is not installed. Install it from Boswas Control Center or with "
                             "\"boswas-android runtime install\".")


def require_initialized(ctx: Context) -> None:
    require_installed(ctx)
    if not initialized(ctx):
        raise NotInitialized("The Android runtime is installed but not set up yet: run \"boswas-android runtime "
                             "init\" (downloads the Android system images).")


def require_running(ctx: Context) -> None:
    require_initialized(ctx)
    if not session_state(ctx)["running"]:
        raise NotRunning("Android is not running. Start it with \"boswas-android session start\".")


def require_supported_for_install(ctx: Context) -> None:
    compat = checks.compatibility(ctx.root, ctx.host)
    failures = checks.blocking_failures(compat)
    if failures:
        raise Unsupported("This computer cannot run the Android runtime: "
                          + " ".join(f["detail"] for f in failures), problems=[f["detail"] for f in failures])


# --- privileged operations (boswas-sysops) ----------------------------------------------------------

RUNTIME_OPERATIONS = {"install": "android-runtime-install", "init": "android-init", "update": "android-update",
                      "repair": "android-repair", "remove": "android-runtime-remove"}


def runtime_operation(ctx: Context, action: str, purge_data: bool = False, on_event=None, cancel_event=None):
    if action not in RUNTIME_OPERATIONS:
        raise InvalidArgument(f"unknown runtime action {action!r}")
    if action == "install":
        require_supported_for_install(ctx)
    elif action == "init":
        require_installed(ctx)
    elif action in ("update", "repair"):
        require_initialized(ctx)
    args = ("--purge-data",) if action == "remove" and purge_data else ()
    outcome = sysops.run(ctx.runner, RUNTIME_OPERATIONS[action], args, on_event, cancel_event)
    result = outcome.result or {}
    userlog.write(ctx.host, f"runtime-{action}", str(result.get("code")), str(result.get("message", "")))
    return outcome


def service_operation(ctx: Context, verb: str, on_event=None, cancel_event=None):
    if verb not in ("start", "stop", "restart"):
        raise InvalidArgument(f"unknown service action {verb!r}")
    require_installed(ctx)
    outcome = sysops.run(ctx.runner, "android-service", (verb,), on_event, cancel_event)
    result = outcome.result or {}
    userlog.write(ctx.host, f"service-{verb}", str(result.get("code")), str(result.get("message", "")))
    return outcome


def ensure_service(ctx: Context) -> None:
    if service_active(ctx) is True:
        return
    outcome = sysops.run(ctx.runner, "android-service", ("start",))
    if not (outcome.result or {}).get("ok"):
        raise Failed("The Android container service could not be started: "
                     + str((outcome.result or {}).get("message", "unknown error")))


# --- session ------------------------------------------------------------------------------------------

def _wait(ctx: Context, predicate, seconds: float) -> bool:
    deadline = ctx.monotonic() + seconds
    while True:
        if predicate():
            return True
        if ctx.monotonic() >= deadline:
            return False
        ctx.sleep(POLL)


def session_start(ctx: Context) -> dict:
    require_initialized(ctx)
    wayland = checks.wayland_check(ctx.host)
    if wayland["status"] == checks.FAIL:
        raise Unsupported(wayland["detail"])
    if session_state(ctx)["running"]:
        return {"schema": JSON_SCHEMA, "session": {"running": True, "already": True}}
    ensure_service(ctx)
    try:
        ctx.runner.start(runner_mod.argv("waydroid", "session", "start"), log_path=userlog.path(ctx.host))
    except runner_mod.RunnerError as exc:
        userlog.write(ctx.host, "session-start", "FAILED", exc.message)
        raise Failed(f"Android could not be started: {exc.message}") from None
    if not _wait(ctx, lambda: session_state(ctx)["running"], SESSION_START_WAIT):
        userlog.write(ctx.host, "session-start", "FAILED", "not running after wait")
        raise Failed("Android did not finish starting within two minutes. See \"boswas-android logs\".")
    userlog.write(ctx.host, "session-start", "OK")
    return {"schema": JSON_SCHEMA, "session": {"running": True, "already": False}}


def session_stop(ctx: Context) -> dict:
    require_installed(ctx)
    if not initialized(ctx) or not session_state(ctx)["running"]:
        return {"schema": JSON_SCHEMA, "session": {"running": False, "already": True}}
    try:
        _waydroid(ctx, "session", "stop", timeout=60.0)
    except runner_mod.RunnerError as exc:
        raise Failed(f"Android could not be stopped: {exc.message}") from None
    stopped = _wait(ctx, lambda: not session_state(ctx)["running"], 30.0)
    userlog.write(ctx.host, "session-stop", "OK" if stopped else "FAILED")
    if not stopped:
        raise Failed("Android is still running after the stop request.")
    return {"schema": JSON_SCHEMA, "session": {"running": False, "already": False}}


# --- apps ---------------------------------------------------------------------------------------------

def app_list(ctx: Context) -> list[dict]:
    require_running(ctx)
    try:
        result = _waydroid(ctx, "app", "list", timeout=60.0)
    except runner_mod.RunnerError as exc:
        raise Failed(f"The Android app list could not be read: {exc.message}") from None
    text = result.stdout + "\n" + result.stderr
    apps = wd.parse_app_list(text)
    if not apps and wd.SESSION_STOPPED in text.lower():
        raise NotRunning("Android is not running. Start it with \"boswas-android session start\".")
    return [{"package": a["package"], "name": a["name"], "version": a["version"]} for a in apps]


def _installed_packages(ctx: Context) -> set[str]:
    try:
        return {a["package"] for a in app_list(ctx)}
    except (Failed, NotRunning):
        return set()


def apk_install(ctx: Context, path: str, label: str | None = None) -> dict:
    doc = apk.inspect(path, runtime_abis(ctx))
    if not doc["compatible"]:
        userlog.write(ctx.host, "apk-install", "REFUSED", f"{doc['package']}: {'; '.join(doc['problems'])}")
        raise Refused("This APK cannot be installed: " + " ".join(doc["problems"]), code="INCOMPATIBLE",
                      problems=doc["problems"])
    require_running(ctx)
    package = doc["package"]
    before = package in _installed_packages(ctx)
    try:
        result = _waydroid(ctx, "app", "install", doc["path"], timeout=APP_TIMEOUT)
    except runner_mod.RunnerError as exc:
        raise Failed(f"The APK could not be handed to Android: {exc.message}") from None
    error = wd.output_error(result.stdout + "\n" + result.stderr)
    if result.code != 0 or error:
        userlog.write(ctx.host, "apk-install", "FAILED", f"{package}: {error or result.code}")
        raise Failed(f"Android did not install {package}: {error or f'waydroid exit status {result.code}'}.")
    present = _wait(ctx, lambda: package in _installed_packages(ctx), INSTALL_CONFIRM_WAIT)
    if not present:
        userlog.write(ctx.host, "apk-install", "FAILED", f"{package}: not listed after install")
        raise Failed(f"Android did not report {package} as installed. It may have rejected the APK (signature, "
                     "Android version or storage); see \"boswas-android logs\".")
    userlog.write(ctx.host, "apk-install", "OK", f"{package} {doc['version_name']} sha256={doc['sha256']}")
    note = ("Android reported the app as installed. It was installed before, so this was an update or reinstall; "
            "Android does not report app versions to Boswas OS." if before else
            "Android reported the app as installed; it appears in the application launcher.")
    return {"schema": JSON_SCHEMA, "installed": {"package": package, "label": label or doc["label"],
                                                  "version_name": doc["version_name"], "updated": before,
                                                  "message": note}, "apk": doc}


def app_remove(ctx: Context, package: str) -> dict:
    valid_package(package)
    require_running(ctx)
    try:
        result = _waydroid(ctx, "app", "remove", package, timeout=APP_TIMEOUT)
    except runner_mod.RunnerError as exc:
        raise Failed(f"The removal could not be requested: {exc.message}") from None
    error = wd.output_error(result.stdout + "\n" + result.stderr)
    if result.code != 0 or error:
        userlog.write(ctx.host, "app-remove", "FAILED", f"{package}: {error or result.code}")
        raise Failed(f"Android did not remove {package}: {error or f'waydroid exit status {result.code}'}.")
    gone = _wait(ctx, lambda: package not in _installed_packages(ctx), REMOVE_CONFIRM_WAIT)
    userlog.write(ctx.host, "app-remove", "OK" if gone else "FAILED", package)
    if not gone:
        raise Failed(f"{package} is still installed after the removal request.")
    return {"schema": JSON_SCHEMA, "removed": {"package": package}}


def app_launch(ctx: Context, package: str) -> dict:
    valid_package(package)
    require_initialized(ctx)
    wayland = checks.wayland_check(ctx.host)
    if wayland["status"] == checks.FAIL:
        raise Unsupported(wayland["detail"])
    if not session_state(ctx)["running"]:
        ensure_service(ctx)                 # `waydroid app launch` starts the session itself
    try:
        ctx.runner.start(runner_mod.argv("waydroid", "app", "launch", package), log_path=userlog.path(ctx.host))
    except runner_mod.RunnerError as exc:
        userlog.write(ctx.host, "app-launch", "FAILED", f"{package}: {exc.message}")
        raise Failed(f"{package} could not be started: {exc.message}") from None
    userlog.write(ctx.host, "app-launch", "OK", package)
    return {"schema": JSON_SCHEMA, "launched": {"package": package}}


# --- F-Droid --------------------------------------------------------------------------------------------

def fdroid_install(ctx: Context) -> dict:
    conf = ctx.conf
    if not conf.valid:
        raise Failed("The shipped Android runtime facts are invalid: " + "; ".join(conf.problems()))
    require_running(ctx)
    cache_home = ctx.host.environ.get("XDG_CACHE_HOME")
    if not (cache_home and os.path.isabs(cache_home)):
        home = ctx.host.home()
        if not home:
            raise Failed("HOME is not set; F-Droid cannot be downloaded.")
        cache_home = os.path.join(home, ".cache")
    path = fdroid.download(conf.fdroid_url, os.path.join(cache_home, "boswas", "android"), ctx.opener)
    try:
        fdroid.verify(path, conf.fdroid_cert_sha256, runtime_abis(ctx))
        result = apk_install(ctx, path, label="F-Droid")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    userlog.write(ctx.host, "fdroid-install", "OK")
    result["installed"]["store"] = googleplay.ALTERNATIVE_LABELS["fdroid"]
    return result


# --- logs and cleanup -------------------------------------------------------------------------------------

def logs(ctx: Context, lines: int = 100) -> dict:
    lines = max(1, min(int(lines), 2000))
    own_path = userlog.path(ctx.host)
    own = None
    if own_path:
        try:
            with open(own_path, "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - 256 * 1024))
                own = handle.read().decode("utf-8", errors="replace")
        except OSError:
            own = None
    runtime_text = ctx.root.read_tail(config.WAYDROID_LOG, 256 * 1024)
    return {"schema": JSON_SCHEMA,
            "boswas": {"path": own_path, "lines": own.splitlines()[-lines:] if own is not None else []},
            "waydroid": {"path": config.WAYDROID_LOG, "readable": runtime_text is not None,
                         "lines": runtime_text.splitlines()[-lines:] if runtime_text is not None else []}}


def cleanup_user(ctx: Context) -> dict:
    if runtime_installed(ctx)[0]:
        raise Refused("The Android runtime is still installed. Remove it first (\"boswas-android runtime remove\"); "
                      "removing the runtime's apps' data while it is installed would break it.")
    data_home = ctx.host.data_home()
    if not data_home:
        raise Failed("The user's data directory is unknown (HOME is not set).")
    removed, failed_entries = cleanup.remove_desktop_entries(data_home)
    existed, failed_data = cleanup.remove_user_data(data_home)
    userlog.write(ctx.host, "cleanup", "OK" if not (failed_entries or failed_data) else "PARTIAL",
                  f"entries={len(removed)} data={existed}")
    return {"schema": JSON_SCHEMA, "cleanup": {"desktop_entries_removed": len(removed),
                                                "user_data_removed": existed and not failed_data,
                                                "not_removed": failed_entries + failed_data}}
