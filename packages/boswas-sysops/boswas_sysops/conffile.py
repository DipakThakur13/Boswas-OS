"""KEY="value" configuration files (apps.conf, runtime.conf). Never evaluated.

Same format as the other Boswas configuration files: blank lines and #
comments are ignored, values are split with shell quoting rules (no
expansion, no command substitution) and joined with single spaces.
"""

from __future__ import annotations

import re
import shlex

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse(text: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY.fullmatch(key):
            continue
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError:
            continue
        result[key] = " ".join(parts)
    return result
