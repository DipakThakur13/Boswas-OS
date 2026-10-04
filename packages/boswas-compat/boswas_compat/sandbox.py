"""bubblewrap sandbox for one Windows application.

What exists inside the sandbox:

  /usr, /etc              read-only (system files; the merged-/usr symlinks)
  /proc, /dev             new instances (pid namespace; minimal device nodes)
  /tmp, /dev/shm          private tmpfs
  /sys/devices/system/*   read-only CPU topology (Wine reads it)
  /run/user/<uid>         private tmpfs, only the sockets granted below
  /var/lib/boswas/wine/<id>   the application's own state (prefix, home, installer)

Only when granted (manifest sandbox, or policy for unlisted apps):
  network   the host network namespace (otherwise: loopback only)
  display   the X11 socket of $DISPLAY and its Xauthority file
  audio     the PulseAudio/PipeWire socket
  gpu       /dev/dri and the sysfs device tree Mesa needs
  folders   the user's Documents, Downloads, ... at <view>/home/<Name>

Never: the user's home directory, other applications' prefixes, the session
or system D-Bus, /run, /var, /home, /media, block devices, input devices.
The environment is cleared; only the variables set here exist.
"""

from __future__ import annotations

import os
import pwd
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .manifest import env_key_allowed
from .policy import Grants

FOLDER_XDG = {
    "documents": ("XDG_DOCUMENTS_DIR", "Documents"),
    "downloads": ("XDG_DOWNLOAD_DIR", "Downloads"),
    "desktop": ("XDG_DESKTOP_DIR", "Desktop"),
    "pictures": ("XDG_PICTURES_DIR", "Pictures"),
    "music": ("XDG_MUSIC_DIR", "Music"),
    "videos": ("XDG_VIDEOS_DIR", "Videos"),
}
_DISPLAY_RE = re.compile(r"^(?:unix)?:(\d{1,4})(?:\.\d+)?$")
_LANG_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_TZ_RE = re.compile(r"^:?[A-Za-z0-9_+./-]{1,64}$")


@dataclass(frozen=True)
class Runtime:
    """Wine runtime facts (compatibility/wine/runtime.conf)."""

    loader: str = "/usr/lib/wine/wine64"
    server: str = "/usr/lib/wine/wineserver64"
    wine_major: str = "10"
    architectures: tuple[str, ...] = ("x86_64",)
    winearch: str = "win64"
    winedebug: str = "-all"
    dll_overrides: str = "mscoree,mshtml=;winemenubuilder.exe=d"


@dataclass(frozen=True)
class HostSession:
    """Facts about the invoking user's session, read once per command."""

    uid: int
    username: str
    home: Path
    display: str | None = None
    xauthority: Path | None = None
    runtime_dir: Path | None = None
    lang: str = "C.UTF-8"
    tz: str | None = None

    @classmethod
    def current(cls, home: Path, environ: dict | None = None) -> "HostSession":
        env = os.environ if environ is None else environ
        uid = os.getuid()
        try:
            username = pwd.getpwuid(uid).pw_name
        except KeyError:
            username = f"uid{uid}"
        xauth = env.get("XAUTHORITY")
        rtdir = env.get("XDG_RUNTIME_DIR")
        lang = env.get("LANG", "C.UTF-8")
        tz = env.get("TZ")
        return cls(
            uid=uid,
            username=username,
            home=home,
            display=env.get("DISPLAY") or None,
            xauthority=Path(xauth) if xauth else None,
            runtime_dir=Path(rtdir) if rtdir else None,
            lang=lang if _LANG_RE.fullmatch(lang) else "C.UTF-8",
            tz=tz if tz and _TZ_RE.fullmatch(tz) else None,
        )


def _owned_by(path: Path, uid: int, kind: int) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return st.st_uid == uid and stat.S_IFMT(st.st_mode) == kind


def x11_socket(display: str | None) -> Path | None:
    """Local X11 socket for $DISPLAY (":0", ":1.0", "unix:0"); None for remote displays."""
    if not display:
        return None
    m = _DISPLAY_RE.fullmatch(display)
    return Path(f"/tmp/.X11-unix/X{m.group(1)}") if m else None


def user_folder(home: Path, folder: str) -> Path | None:
    """The user's XDG folder for a grant, if it exists and is a real directory below home."""
    key, default = FOLDER_XDG[folder]
    target = home / default
    dirs_file = home / ".config" / "user-dirs.dirs"
    try:
        for line in dirs_file.read_text(encoding="utf-8", errors="replace").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == key:
                value = value.strip().strip('"')
                if value.startswith("$HOME/"):
                    target = home / value[len("$HOME/"):]
                elif value.startswith("/"):
                    target = Path(value)
    except OSError:
        pass
    try:
        resolved = target.resolve(strict=True)
        home_resolved = home.resolve(strict=True)
    except OSError:
        return None
    # Never the home directory itself or anything outside it.
    if resolved == home_resolved or home_resolved not in resolved.parents:
        return None
    if not resolved.is_dir():
        return None
    return resolved


_DRM_NODE_RE = re.compile(r"^(card|renderD)\d+$")


def gpu_sysfs_dirs() -> list[str]:
    """PCI device directories of the GPUs (/sys/class/drm/<node>/device)."""
    root = paths.sysroot()
    drm = paths.system_path("/sys/class/drm")
    try:
        names = sorted(os.listdir(drm))
    except OSError:
        return []
    found = set()
    for name in names:
        if not _DRM_NODE_RE.fullmatch(name):
            continue
        real = Path(os.path.realpath(drm / name / "device"))
        try:
            host = "/" + str(real.relative_to(root.resolve()))
        except ValueError:
            continue
        if host.startswith("/sys/devices/pci") and real.is_dir():
            found.add(host)
    return sorted(found)


def _merged_usr_args() -> list[str]:
    args = []
    for name in ("bin", "sbin", "lib", "lib64"):
        host = paths.system_path(f"/{name}")
        try:
            link = os.readlink(host)
        except OSError:
            if host.is_dir():
                args += ["--ro-bind", f"/{name}", f"/{name}"]
            continue
        args += ["--symlink", link, f"/{name}"]
    return args


def build_command(*, app_id: str, appdir: Path, session: HostSession, grants: Grants, runtime: Runtime,
                  command: list[str], cwd: str | None = None, extra_env: dict[str, str] | None = None,
                  disable_userns: bool = True, require_apparmor: bool = True) -> list[str]:
    """bwrap argv that runs ``command`` (via the AppArmor-attached runner) for one application."""
    view = paths.view_dir(app_id)
    home_view = f"{view}/home"
    uid = session.uid
    run_dir = f"/run/user/{uid}"

    argv = [paths.BWRAP, "--unshare-all", "--unshare-user"]
    if disable_userns:
        argv.append("--disable-userns")
    if grants.network:
        argv.append("--share-net")
    argv += ["--die-with-parent", "--new-session", "--cap-drop", "ALL"]

    # System: read-only.
    argv += ["--ro-bind", "/usr", "/usr", *_merged_usr_args(), "--ro-bind", "/etc", "/etc"]
    if grants.network:
        # /etc/resolv.conf may point into /run (NetworkManager, systemd-resolved).
        try:
            resolv = Path(os.path.realpath(paths.system_path("/etc/resolv.conf")))
            if str(resolv).startswith("/run/"):
                argv += ["--ro-bind-try", str(resolv), str(resolv)]
        except OSError:
            pass
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]

    for sysdir in ("/sys/devices/system/cpu", "/sys/devices/system/node"):
        argv += ["--ro-bind-try", sysdir, sysdir]
    if grants.gpu:
        # The DRM device nodes and only the GPUs' own PCI directories: never all
        # of /sys/devices (network devices, disks and their serial numbers).
        argv += ["--dev-bind-try", "/dev/dri", "/dev/dri"]
        for sysdir in ("/sys/class/drm", "/sys/dev/char", *gpu_sysfs_dirs()):
            argv += ["--ro-bind-try", sysdir, sysdir]

    argv += ["--perms", "0700", "--dir", run_dir]

    env: dict[str, str] = {}
    if grants.display:
        sock = x11_socket(session.display)
        if sock is not None and paths.system_path(str(sock)).exists():
            argv += ["--ro-bind", str(sock), str(sock)]
            env["DISPLAY"] = session.display or ""
            if session.xauthority and _owned_by(session.xauthority, uid, stat.S_IFREG):
                argv += ["--ro-bind", str(session.xauthority), f"{run_dir}/X11/Xauthority"]
                env["XAUTHORITY"] = f"{run_dir}/X11/Xauthority"
    if grants.audio and session.runtime_dir:
        pulse = session.runtime_dir / "pulse" / "native"
        if _owned_by(pulse, uid, stat.S_IFSOCK):
            argv += ["--ro-bind", str(pulse), f"{run_dir}/pulse/native"]
            env["PULSE_SERVER"] = f"unix:{run_dir}/pulse/native"

    # The application's own state, then the folders it was granted.
    argv += ["--bind", str(appdir / "sandbox"), view]
    for folder in grants.folders:
        host = user_folder(session.home, folder)
        if host is not None:
            argv += ["--bind", str(host), f"{home_view}/{FOLDER_XDG[folder][1]}"]

    argv += ["--chdir", cwd or home_view, "--clearenv"]
    env.update({
        "HOME": home_view,
        "USER": session.username,
        "LOGNAME": session.username,
        "PATH": "/usr/bin:/bin",
        "LANG": session.lang,
        "XDG_RUNTIME_DIR": run_dir,
        "XDG_CONFIG_HOME": f"{home_view}/.config",
        "XDG_DATA_HOME": f"{home_view}/.local/share",
        "XDG_CACHE_HOME": f"{home_view}/.cache",
        "WINEPREFIX": f"{view}/prefix",
        "WINEARCH": runtime.winearch,
        "WINELOADER": runtime.loader,
        "WINESERVER": runtime.server,
        "WINEDEBUG": runtime.winedebug,
        "WINEDLLOVERRIDES": runtime.dll_overrides,
        # The runner refuses to start Wine unconfined unless both this and the
        # policy file it reads itself say "no".
        "BOSWAS_WINAPP_REQUIRE_APPARMOR": "yes" if require_apparmor else "no",
    })
    if session.tz:
        env["TZ"] = session.tz
    for key, value in (extra_env or {}).items():
        if not env_key_allowed(key):
            continue    # validated manifests never contain these; enforce it here too
        if key == "WINEDLLOVERRIDES":
            # Manifest overrides plus the runtime's (manifests can never
            # change winemenubuilder; see manifest.validate).
            env[key] = f"{value};{runtime.dll_overrides}"
        else:
            env[key] = value
    for key in sorted(env):
        argv += ["--setenv", key, env[key]]
    argv += ["--", paths.RUNNER, *command]
    return argv
