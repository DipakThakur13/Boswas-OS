"""Per-user application store: ~/.local/share/boswas/wine/<application-id>/.

  <id>/                0700  owned by the user
    app.json           installation record (not visible to the application)
    logs/              install, launch and repair logs (not visible)
    .lock              held while an operation or the application runs
    sandbox/           everything the application can see and change; bound
                       at /var/lib/boswas/wine/<id>/ inside its sandbox
      prefix/          WINEPREFIX
      home/            HOME inside the sandbox
      installer/       the verified installer copy, during installation only

The application can only modify sandbox/. Everything that controls the
sandbox (records, grants) lives outside it or is recomputed from the
catalog and the policy at every start.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pwd
import secrets
import shutil
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from . import RECORD_SCHEMA, paths
from .errors import Busy, NotFound, Refused
from .manifest import valid_id

MAX_RECORD_BYTES = 1024 * 1024
KEEP_LOGS = 10
# Fields of a record that the all-users inventory may report.
INVENTORY_FIELDS = ("id", "name", "version", "publisher", "status", "state", "installed_at")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _check_dir(path: Path) -> None:
    """path must be a real directory (not a symlink) owned by us, mode 0700."""
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{path} is not a directory (symbolic links are not accepted)", reason="unsafe-path")
    if st.st_uid != os.getuid():
        raise Refused(f"{path} is not owned by the current user", reason="unsafe-path")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700, follow_symlinks=False)


def _mkdir(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _check_dir(path)


def read_json_safely(path: Path, owner: int | None = None) -> dict | None:
    """Read a small JSON object without following symlinks; None if unusable."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_RECORD_BYTES:
            return None
        if owner is not None and st.st_uid != owner:
            return None
        data = os.read(fd, MAX_RECORD_BYTES + 1)
    finally:
        os.close(fd)
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


class AppStore:
    def __init__(self, home: Path):
        self.home = home
        self.root = home / paths.USER_DATA_SUBDIR

    # --- layout -------------------------------------------------------------
    def ensure_root(self) -> None:
        parent = self.home
        for part in paths.USER_DATA_SUBDIR.split("/"):
            parent = parent / part
            if part in (".local", "share"):
                # The user's own XDG directories (may be symlinks of their choice).
                parent.mkdir(mode=0o755, exist_ok=True)
            else:
                _mkdir(parent)

    def app_dir(self, app_id: str) -> Path:
        if not valid_id(app_id):
            raise NotFound(f"invalid application ID {app_id!r}")
        return self.root / app_id

    def exists(self, app_id: str) -> bool:
        return os.path.lexists(self.app_dir(app_id))

    def create(self, app_id: str) -> Path:
        appdir = self.app_dir(app_id)
        try:
            appdir.mkdir(mode=0o700)
        except FileExistsError:
            raise Refused(f"{app_id} is already installed (use 'boswas-winapp remove {app_id}' "
                          f"or 'boswas-winapp repair {app_id}')", reason="already-installed")
        _check_dir(appdir)
        for sub in ("logs", "sandbox", "sandbox/prefix", "sandbox/home", "sandbox/installer"):
            _mkdir(appdir / sub)
        return appdir

    def check_layout(self, app_id: str) -> Path:
        appdir = self.app_dir(app_id)
        if not os.path.lexists(appdir):
            raise NotFound(f"{app_id} is not installed")
        _check_dir(appdir)
        for sub in ("logs", "sandbox"):
            _mkdir(appdir / sub)
        return appdir

    def new_staging(self) -> Path:
        self.ensure_root()
        staging = self.root / ".staging"
        _mkdir(staging)
        path = staging / secrets.token_hex(8)
        path.mkdir(mode=0o700)
        return path

    # --- records ----------------------------------------------------------------
    def read_record(self, app_id: str) -> dict | None:
        appdir = self.app_dir(app_id)
        return read_json_safely(appdir / "app.json", owner=os.getuid())

    def write_record(self, app_id: str, record: dict) -> None:
        appdir = self.app_dir(app_id)
        record = {"schema": RECORD_SCHEMA, **{k: v for k, v in record.items() if k != "schema"}}
        record["updated_at"] = utc_now()
        tmp = appdir / f".app.json.{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, appdir / "app.json")

    def list_ids(self) -> list[str]:
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return []
        return [n for n in names if valid_id(n) and (self.root / n).is_dir() and not (self.root / n).is_symlink()]

    # --- locking ----------------------------------------------------------------
    @contextmanager
    def lock(self, app_id: str, *, wait: bool = False):
        appdir = self.app_dir(app_id)
        fd = os.open(appdir / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except OSError as exc:
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise Busy(f"{app_id} is running or another boswas-winapp operation is using it")
                raise
            yield
        finally:
            os.close(fd)

    def is_locked(self, app_id: str) -> bool:
        try:
            with self.lock(app_id):
                return False
        except Busy:
            return True
        except OSError:
            return False

    # --- logs ---------------------------------------------------------------------
    def new_log(self, app_id: str, kind: str) -> Path:
        logs = self.app_dir(app_id) / "logs"
        existing = sorted(p for p in logs.glob(f"{kind}-*.log") if p.is_file() and not p.is_symlink())
        for old in existing[:-(KEEP_LOGS - 1)] if len(existing) >= KEEP_LOGS else []:
            old.unlink(missing_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = logs / f"{kind}-{stamp}.log"
        n = 1
        while path.exists():
            n += 1
            path = logs / f"{kind}-{stamp}-{n}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        os.close(fd)
        return path

    def logs(self, app_id: str, kind: str | None = None) -> list[Path]:
        logs = self.app_dir(app_id) / "logs"
        pattern = f"{kind}-*.log" if kind else "*.log"
        return sorted((p for p in logs.glob(pattern) if p.is_file() and not p.is_symlink()),
                      key=lambda p: p.stat().st_mtime)

    # --- removal --------------------------------------------------------------------
    def remove(self, app_id: str) -> None:
        appdir = self.app_dir(app_id)
        if appdir.is_symlink():
            appdir.unlink()
            return
        if not appdir.exists():
            raise NotFound(f"{app_id} is not installed")
        # rmtree never follows symbolic links the application may have created.
        shutil.rmtree(appdir)

    def discard_staging(self, path: Path) -> None:
        if path.parent == self.root / ".staging":
            shutil.rmtree(path, ignore_errors=True)


def list_all_users(min_uid: int = 1000) -> list[dict]:
    """Inventory of every user's Windows applications (root only).

    Reads only records owned by their user, never following symbolic links,
    and reports only INVENTORY_FIELDS.
    """
    if os.geteuid() != 0:
        raise Refused("--all-users needs root (it reads every user's installation records)",
                      reason="needs-root")
    result = []
    for entry in sorted(pwd.getpwall(), key=lambda e: e.pw_uid):
        if entry.pw_uid < min_uid or entry.pw_uid == 65534 or not entry.pw_dir.startswith("/"):
            continue
        root = Path(entry.pw_dir) / paths.USER_DATA_SUBDIR
        try:
            if root.is_symlink() or not root.is_dir() or root.lstat().st_uid != entry.pw_uid:
                continue
            names = sorted(os.listdir(root))
        except OSError:
            continue
        apps = []
        for name in names:
            if not valid_id(name):
                continue
            record = read_json_safely(root / name / "app.json", owner=entry.pw_uid)
            if not record or record.get("id") != name:
                continue
            apps.append({k: record.get(k) for k in INVENTORY_FIELDS})
        if apps:
            result.append({"user": entry.pw_name, "uid": entry.pw_uid, "applications": apps})
    return result

