"""The desktop background: the ``wallpaper`` key of the compositor's configuration (standard library only).

The Boswas desktop reads its look from the ``[appearance]`` table of
``$XDG_CONFIG_HOME/boswas/compositor.toml``. Setting a picture as the
background means:

1. the picture is copied to ``$XDG_DATA_HOME/boswas/wallpapers`` (the name
   carries a digest of the content), so it stays when the original is
   moved or deleted;
2. ``wallpaper = "PATH"`` is written into ``[appearance]``;
3. the compositor is asked to read its configuration again
   (``boswas-compositor --send reload-config``).

The configuration file belongs to the user, who may have written it by
hand, so it is edited as text: ``set_key`` changes or adds the one key and
keeps every other character (comments, order, spacing). Before anything is
written, the result is parsed again and compared with the original; if
anything but the one key differs, or the file is no valid TOML to begin
with, nothing is written and ``ConfigError`` says why.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .fileutil import atomic_write

MAX_CONFIG_BYTES = 1024 * 1024
COMPOSITOR = "boswas-compositor"
RELOAD_ARGUMENTS = ("--send", "reload-config")
RELOAD_TIMEOUT = 5.0
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class ConfigError(ValueError):
    """The configuration could not be changed safely; the text says why in plain words."""


# --- TOML as text ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Statement:
    kind: str                      # "table", "array", "pair"
    name: tuple[str, ...]
    value_start: int = 0
    value_end: int = 0
    line_end: int = 0              # just after the line break that ends the statement


def toml_string(value: str) -> str:
    """``value`` as a TOML basic string."""
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        elif 0xD800 <= code <= 0xDFFF:
            raise ConfigError("The file name has characters that cannot be written to the configuration.")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _skip_blank(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def _line_end(text: str, i: int) -> int:
    j = text.find("\n", i)
    return len(text) if j < 0 else j + 1


def _string_end(text: str, i: int) -> int:
    """The index after the string that starts at ``i`` (any of the four TOML string forms)."""
    quote = text[i]
    if text.startswith(quote * 3, i):
        j = i + 3
        while True:
            j = text.find(quote * 3, j)
            if j < 0:
                raise ConfigError("unterminated string")
            if quote == '"':
                backslashes = 0
                k = j - 1
                while k >= i + 3 and text[k] == "\\":
                    backslashes += 1
                    k -= 1
                if backslashes % 2:
                    j += 1
                    continue
            j += 3
            while j < len(text) and text[j] == quote:       # up to two more quotes belong to the content
                j += 1
            return j
    j = i + 1
    while j < len(text):
        ch = text[j]
        if ch == "\n":
            break
        if ch == "\\" and quote == '"':
            j += 2
            continue
        if ch == quote:
            return j + 1
        j += 1
    raise ConfigError("unterminated string")


def _key(text: str, i: int, stop: str) -> tuple[tuple[str, ...], int]:
    """A (dotted) key that starts at ``i`` and ends before ``stop``; returns its parts and the index of ``stop``."""
    parts: list[str] = []
    while True:
        i = _skip_blank(text, i)
        if i >= len(text):
            raise ConfigError("unexpected end in a key")
        if text[i] in "\"'":
            end = _string_end(text, i)
            try:
                parts.append(tomllib.loads(f"k = {text[i:end]}")["k"])
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError("a key that cannot be read") from exc
            i = end
        else:
            match = _BARE_KEY.match(text, i)
            if match is None:
                raise ConfigError("a key that cannot be read")
            parts.append(match.group())
            i = match.end()
        i = _skip_blank(text, i)
        if i < len(text) and text[i] == ".":
            i += 1
            continue
        if text.startswith(stop, i):
            return tuple(parts), i
        raise ConfigError("a key that cannot be read")


def _value_end(text: str, i: int) -> int:
    """The index after the value that starts at ``i``."""
    depth = 0
    last = i
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            i = _string_end(text, i)
            last = i
            continue
        if ch == "#":
            if depth == 0:
                break
            i = _line_end(text, i)
            continue
        if ch == "\n":
            if depth == 0:
                break
            i += 1
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth < 0:
                raise ConfigError("unbalanced brackets")
        i += 1
        if ch not in " \t\r":
            last = i
    if depth != 0:
        raise ConfigError("unbalanced brackets")
    return last


def _statements(text: str) -> list[_Statement]:
    found: list[_Statement] = []
    i = 0
    while i < len(text):
        i = _skip_blank(text, i)
        if i >= len(text):
            break
        ch = text[i]
        if ch in "\r\n#":
            i = _line_end(text, i)
            continue
        if text.startswith("[[", i):
            name, close = _key(text, i + 2, "]]")
            i = _line_end(text, close)
            found.append(_Statement("array", name, line_end=i))
            continue
        if ch == "[":
            name, close = _key(text, i + 1, "]")
            i = _line_end(text, close)
            found.append(_Statement("table", name, line_end=i))
            continue
        name, equals = _key(text, i, "=")
        start = _skip_blank(text, equals + 1)
        end = _value_end(text, start)
        if end <= start:
            raise ConfigError("a key without a value")
        i = _line_end(text, end)
        found.append(_Statement("pair", name, start, end, i))
    return found


def _without(document: dict, table: str, key: str) -> dict:
    copy = dict(document)
    if isinstance(copy.get(table), dict):
        inner = dict(copy[table])
        inner.pop(key, None)
        copy[table] = inner
        if not inner:
            del copy[table]
    return copy


def set_key(text: str, table: str, key: str, value: str) -> str:
    """``text`` (a TOML document) with ``[table] key`` set to the string ``value``; the rest is untouched.

    ConfigError when ``text`` is not valid TOML, or defines the table in a
    way that cannot be extended by adding a line (an inline table).
    """
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"The file is not valid TOML ({exc}); correct it first.") from exc
    if table in before and not isinstance(before[table], dict):
        raise ConfigError(f"'{table}' is not a table in this file.")
    newline = "\r\n" if "\r\n" in text else "\n"
    literal = toml_string(value)
    try:
        statements = _statements(text)
    except ConfigError as exc:
        raise ConfigError(f"The file has a form this program does not edit ({exc}).") from exc

    current: tuple[str, ...] | None = ()
    target: _Statement | None = None           # the pair to rewrite
    header: _Statement | None = None           # the [table] header
    last_in_table: _Statement | None = None    # where a new line goes inside [table]
    last_dotted: _Statement | None = None      # the last "table.x = ..." of the root table
    inline = False
    for statement in statements:
        if statement.kind == "table":
            current = statement.name
            if current == (table,):
                header = last_in_table = statement
            continue
        if statement.kind == "array":
            current = None
            continue
        if current == (table,):
            last_in_table = statement
            if statement.name == (key,):
                target = statement
        elif current == ():
            if statement.name == (table, key):
                target = statement
            elif len(statement.name) > 1 and statement.name[0] == table:
                last_dotted = statement
            elif statement.name == (table,):
                inline = True

    if target is not None:
        result = text[:target.value_start] + literal + text[target.value_end:]
    elif header is not None:
        at = last_in_table.line_end
        prefix = "" if at == 0 or text[at - 1] == "\n" else newline
        result = text[:at] + prefix + f"{key} = {literal}{newline}" + text[at:]
    elif last_dotted is not None:
        at = last_dotted.line_end
        prefix = "" if text[at - 1] == "\n" else newline
        result = text[:at] + prefix + f"{table}.{key} = {literal}{newline}" + text[at:]
    elif inline:
        raise ConfigError(f"'{table}' is written as an inline table; add {key} = {literal} to it by hand.")
    else:
        gap = "" if not text else (newline if text.endswith("\n") else newline * 2)
        result = text + gap + f"[{table}]{newline}{key} = {literal}{newline}"

    try:
        after = tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("The file has a form this program does not edit.") from exc
    if (not isinstance(after.get(table), dict) or after[table].get(key) != value
            or repr(_without(after, table, key)) != repr(_without(before, table, key))):
        raise ConfigError("The file has a form this program does not edit.")
    return result


# --- files -------------------------------------------------------------------------------------------------

def config_path() -> Path:
    """``$XDG_CONFIG_HOME/boswas/compositor.toml``: the user's desktop configuration."""
    base = os.environ.get("XDG_CONFIG_HOME") or ""
    root = Path(base) if base.startswith("/") else Path.home() / ".config"
    return root / "boswas" / "compositor.toml"


def wallpaper_dir() -> Path:
    """``$XDG_DATA_HOME/boswas/wallpapers``: where the pictures used as background are kept."""
    base = os.environ.get("XDG_DATA_HOME") or ""
    root = Path(base) if base.startswith("/") else Path.home() / ".local" / "share"
    return root / "boswas" / "wallpapers"


def wallpaper_name(original_name: str, content: bytes, extension: str) -> str:
    """The file name of a wallpaper copy: a tame form of the original name, a digest of the content."""
    stem = os.path.splitext(os.path.basename(original_name))[0]
    stem = _SAFE_NAME.sub("-", stem).strip("-.")[:48] or "wallpaper"
    digest = hashlib.sha256(content).hexdigest()[:12]
    return f"{stem}-{digest}.{extension.lstrip('.').lower()}"


def store_wallpaper(original_name: str, content: bytes, extension: str, directory: Path | None = None) -> Path:
    """Write ``content`` (a PNG or JPEG) into the wallpaper directory; returns the file."""
    if extension.lstrip(".").lower() not in ("png", "jpg", "jpeg"):
        raise ConfigError("The desktop shows PNG and JPEG pictures only.")
    folder = directory if directory is not None else wallpaper_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = folder / wallpaper_name(original_name, content, extension)
    if not target.is_file() or target.stat().st_size != len(content):
        atomic_write(target, content, mode=0o644)
    return target


def read_config_text(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ConfigError(f"{path} cannot be read: {exc.strerror or exc}.") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError(f"{path} is too large to be a configuration file.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} is not UTF-8 text.") from exc


def current_wallpaper(path: Path | None = None) -> str:
    """The wallpaper the user's configuration names ("" when none or the file cannot be read)."""
    try:
        document = tomllib.loads(read_config_text(path or config_path()))
    except (ConfigError, tomllib.TOMLDecodeError):
        return ""
    value = document.get("appearance", {}).get("wallpaper") if isinstance(document.get("appearance"), dict) else ""
    return value if isinstance(value, str) else ""


def set_wallpaper(picture: str | os.PathLike, path: Path | None = None) -> Path:
    """Write ``wallpaper = "picture"`` into ``[appearance]`` of the user's configuration; returns the file."""
    picture = os.fspath(picture)
    if not os.path.isabs(picture):
        raise ConfigError("The wallpaper must be an absolute file name.")
    target = path or config_path()
    text = read_config_text(target)
    updated = set_key(text, "appearance", "wallpaper", picture)
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(target, updated.encode("utf-8"))
    except OSError as exc:
        raise ConfigError(f"{target} cannot be written: {exc.strerror or exc}.") from exc
    return target


def reload_compositor(run=subprocess.run) -> tuple[bool, str]:
    """Ask the running compositor to read its configuration again.

    Returns (done, why not). The program is started by name with a fixed
    argument list, without a shell.
    """
    try:
        result = run([COMPOSITOR, *RELOAD_ARGUMENTS], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, timeout=RELOAD_TIMEOUT, check=False)
    except FileNotFoundError:
        return False, "the Boswas desktop is not installed here"
    except subprocess.TimeoutExpired:
        return False, "the desktop did not answer"
    except OSError as exc:
        return False, exc.strerror or str(exc)
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return False, detail[-1][:200] if detail else "the desktop is not running"
    return True, ""
