"""KEY="value" configuration files (policy.conf, runtime.conf). Never evaluated."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            continue
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            continue
        result[key] = " ".join(parts)
    return result


def load(path: Path) -> dict[str, str]:
    try:
        return parse(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}
