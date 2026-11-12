"""File system access through a root ("/" on a device, a temporary directory in the tests).

Every path the helper reads or writes goes through ``Root``; files are
opened without following symbolic links where the helper writes, and
directories it creates get explicit modes.
"""

from __future__ import annotations

import os
import stat


class Root:
    def __init__(self, path: str = "/"):
        self.path = path or "/"

    def __call__(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError(f"not an absolute path: {path!r}")
        return os.path.join(self.path, path.lstrip("/"))

    def read(self, path: str, limit: int = 1024 * 1024) -> str | None:
        try:
            with open(self(path), "rb") as handle:
                return handle.read(limit).decode("utf-8", errors="replace")
        except OSError:
            return None

    def read_bytes(self, path: str, limit: int = 1024 * 1024) -> bytes | None:
        try:
            with open(self(path), "rb") as handle:
                return handle.read(limit)
        except OSError:
            return None

    def exists(self, path: str) -> bool:
        return os.path.lexists(self(path))

    def is_file(self, path: str) -> bool:
        try:
            return stat.S_ISREG(os.lstat(self(path)).st_mode)
        except OSError:
            return False

    def executable(self, path: str) -> bool:
        target = self(path)
        return os.path.isfile(target) and os.access(target, os.X_OK)

    def listdir(self, path: str) -> list[str]:
        try:
            return sorted(os.listdir(self(path)))
        except OSError:
            return []

    def write_file(self, path: str, text: str, mode: int = 0o644) -> None:
        """Replace a file atomically (temporary file + rename), never through a symbolic link."""
        target = self(path)
        directory = os.path.dirname(target)
        os.makedirs(directory, mode=0o755, exist_ok=True)
        tmp = os.path.join(directory, f".{os.path.basename(target)}.boswas-sysops.tmp")
        if os.path.lexists(tmp):
            os.unlink(tmp)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
        try:
            os.fchmod(fd, mode)
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)

    def remove_file(self, path: str) -> bool:
        target = self(path)
        try:
            if stat.S_ISDIR(os.lstat(target).st_mode):
                raise IsADirectoryError(target)
            os.unlink(target)
            return True
        except FileNotFoundError:
            return False

    def tree_size(self, path: str) -> int:
        """Bytes used by regular files below path (symbolic links are not followed)."""
        total = 0
        base = self(path)
        for current, _dirs, files in os.walk(base):
            for name in files:
                try:
                    st = os.lstat(os.path.join(current, name))
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    total += st.st_blocks * 512 if st.st_blocks else st.st_size
        return total

    def free_bytes(self, path: str) -> int | None:
        try:
            st = os.statvfs(self(path))
        except OSError:
            return None
        return st.f_bavail * st.f_frsize
