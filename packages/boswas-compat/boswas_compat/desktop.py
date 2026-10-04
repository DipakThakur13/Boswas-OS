"""Desktop launchers for installed Windows applications.

boswas-winapp writes one entry per application into the user's
~/.local/share/applications. Windows programs themselves can never create
host desktop entries or MIME associations (winemenubuilder is disabled, and
the user's real home is not visible inside the sandbox).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from . import paths
from .executor import sanitize


def entry_path(home: Path, app_id: str) -> Path:
    return home / paths.DESKTOP_SUBDIR / f"boswas-winapp-{app_id}.desktop"


def _value(text: str, limit: int = 120) -> str:
    # Desktop entry values are single lines; drop control characters and
    # the escape character, and keep them short.
    return sanitize(text).replace("\n", " ").replace("\\", "/")[:limit].strip() or "Windows application"


def write_entry(home: Path, app_id: str, name: str, status: str) -> Path:
    path = entry_path(home, app_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={_value(name)}\n"
        f"Comment={_value(f'Windows application, Boswas compatibility status: {status}')}\n"
        f"Exec=boswas-winapp launch {app_id}\n"
        "Icon=application-x-ms-dos-executable\n"
        "Terminal=false\n"
        "Categories=Boswas;\n"
        f"X-Boswas-WinApp-Id={app_id}\n"
    )
    tmp = path.parent / f".{path.name}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)
    return path


def remove_entry(home: Path, app_id: str) -> bool:
    path = entry_path(home, app_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
