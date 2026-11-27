"""The desktop background: the ``wallpaper`` key of the desktop's look (standard library only).

The Boswas desktop reads its look from the ``[appearance]`` table of its
layered configuration. Of the user's two files,
``$XDG_CONFIG_HOME/boswas/appearance.toml`` (written by ``boswas-preset``
and by Control Center: the colour mode and the background) is read after
``$XDG_CONFIG_HOME/boswas/compositor.toml`` and wins. Setting a picture as
the background means, as in Control Center:

1. the picture is copied to ``$XDG_DATA_HOME/boswas/wallpapers`` (the name
   carries a digest of the content), so it stays when the original is
   moved or deleted;
2. ``wallpaper = "PATH"`` is written into ``[appearance]`` of
   appearance.toml, keeping the colour mode and anything else the table
   holds;
3. a ``wallpaper`` left in ``[appearance]`` of compositor.toml (earlier
   versions of Photos wrote it there) is removed: it would come back when a
   preset reset removes appearance.toml;
4. the compositor is asked to read its configuration again
   (``boswas-compositor --send reload-config``).

The files belong to the user, who may have written them by hand, so they
are edited as text: ``set_key`` changes or adds the one key and
``remove_key`` takes one out, keeping every other character (comments,
order, spacing). Before anything is written, the result is parsed again and
compared with the original; if anything but the one key differs, or the
file is no valid TOML to begin with, ``ConfigError`` says why. The
compositor uses appearance.toml only when it holds nothing but the
``[appearance]`` table; a file it would ignore (not TOML, or another table
in it) is replaced by one with the ``[appearance]`` values that can be kept
and the new background. Every file is replaced atomically.
"""

from __future__ import annotations

import hashlib
import json
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
    start: int = 0                 # where the statement begins (its key, after blanks)


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
        key_start = i
        name, equals = _key(text, i, "=")
        start = _skip_blank(text, equals + 1)
        end = _value_end(text, start)
        if end <= start:
            raise ConfigError("a key without a value")
        i = _line_end(text, end)
        found.append(_Statement("pair", name, start, end, i, key_start))
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


def remove_key(text: str, table: str, key: str) -> str:
    """``text`` without ``[table] key`` (the line that sets it goes, comments on it too); the rest is untouched.

    ``text`` unchanged when the key is not set. ConfigError when ``text`` is
    not valid TOML or sets the key in a way that is not one line of its own
    (an inline table).
    """
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"The file is not valid TOML ({exc}); correct it first.") from exc
    if not isinstance(before.get(table), dict) or key not in before[table]:
        return text
    try:
        statements = _statements(text)
    except ConfigError as exc:
        raise ConfigError(f"The file has a form this program does not edit ({exc}).") from exc
    current: tuple[str, ...] | None = ()
    target: _Statement | None = None
    for statement in statements:
        if statement.kind == "table":
            current = statement.name
        elif statement.kind == "array":
            current = None
        elif (current == (table,) and statement.name == (key,)) or (current == () and statement.name == (table, key)):
            target = statement
    if target is None:
        raise ConfigError(f"'{table}' is written as an inline table; remove {key} from it by hand.")
    line_start = text.rfind("\n", 0, target.start) + 1
    result = text[:line_start] + text[target.line_end:]
    try:
        after = tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("The file has a form this program does not edit.") from exc
    inner = dict(before[table])
    del inner[key]
    kept = {**before, table: inner}            # a [table] header stays, perhaps empty
    if _canonical(after) not in (_canonical(kept), _canonical(_without(before, table, key))):
        raise ConfigError("The file has a form this program does not edit.")
    return result


def _canonical(document: dict) -> str:
    """A document as text that tells kinds apart (1, 1.0 and true) and ignores the order of keys."""
    return json.dumps(document, sort_keys=True, default=repr)


# --- files -------------------------------------------------------------------------------------------------

APPEARANCE_HEADER = ("# Written by boswas-preset, Boswas Control Center and Photos; the next preset replaces scheme "
                     "and wallpaper.\n")


def _config_root() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or ""
    return (Path(base) if base.startswith("/") else Path.home() / ".config") / "boswas"


def config_path() -> Path:
    """``$XDG_CONFIG_HOME/boswas/compositor.toml``: the user's desktop configuration."""
    return _config_root() / "compositor.toml"


def appearance_path() -> Path:
    """``$XDG_CONFIG_HOME/boswas/appearance.toml``: the user's colour mode and background, read last."""
    return _config_root() / "appearance.toml"


def _toml_value(value: object) -> str | None:
    """A value of the ``[appearance]`` table as TOML, for the simple kinds it holds; None for anything else."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        try:
            return toml_string(value)
        except ConfigError:
            return None
    return None


def appearance_text(text: str, picture: str) -> str:
    """The content of appearance.toml with ``wallpaper = picture`` (see the module text)."""
    if not text.strip():
        return APPEARANCE_HEADER + set_key("", "appearance", "wallpaper", picture)
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        document = None
    if document is not None and set(document) <= {"appearance"}:
        try:
            return set_key(text, "appearance", "wallpaper", picture)
        except ConfigError:
            pass                                # an inline table: written anew below
    look = document.get("appearance") if isinstance(document, dict) else None
    lines = [APPEARANCE_HEADER.rstrip("\n"), "[appearance]"]
    for key, value in (look.items() if isinstance(look, dict) else ()):
        written = _toml_value(value)
        if key != "wallpaper" and _BARE_KEY.fullmatch(key) and written is not None:
            lines.append(f"{key} = {written}")
    lines.append(f"wallpaper = {toml_string(picture)}")
    return "\n".join(lines) + "\n"


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


def _wallpaper_in(path: Path, only_appearance: bool) -> str | None:
    """The wallpaper a file names: "" for none, None when the desktop does not use the file."""
    try:
        document = tomllib.loads(read_config_text(path))
    except (ConfigError, tomllib.TOMLDecodeError):
        return None
    if only_appearance and not set(document) <= {"appearance"}:
        return None
    value = document.get("appearance", {}).get("wallpaper") if isinstance(document.get("appearance"), dict) else ""
    return value if isinstance(value, str) else ""


def current_wallpaper(path: Path | None = None, appearance: Path | None = None) -> str:
    """The wallpaper the user's files name, appearance.toml before compositor.toml ("" when none)."""
    return (_wallpaper_in(appearance or appearance_path(), True)
            or _wallpaper_in(path or config_path(), False) or "")


def set_wallpaper(picture: str | os.PathLike, path: Path | None = None, appearance: Path | None = None) -> Path:
    """Make ``picture`` the background in appearance.toml and drop a stale copy in compositor.toml (``path``).

    Returns appearance.toml. ConfigError when the picture is not an absolute
    file name or appearance.toml cannot be read or written; nothing is
    written then. A compositor.toml that cannot be edited is left as it is:
    appearance.toml wins over it.
    """
    picture = os.fspath(picture)
    if not os.path.isabs(picture):
        raise ConfigError("The wallpaper must be an absolute file name.")
    target = appearance or appearance_path()
    updated = appearance_text(read_config_text(target), picture)
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(target, updated.encode("utf-8"), mode=0o600)
    except OSError as exc:
        raise ConfigError(f"{target} cannot be written: {exc.strerror or exc}.") from exc
    _drop_stale_wallpaper(path or config_path())
    return target


def _drop_stale_wallpaper(path: Path) -> None:
    """Take ``wallpaper`` out of ``[appearance]`` of compositor.toml, where earlier versions wrote it."""
    try:
        text = read_config_text(path)
        updated = remove_key(text, "appearance", "wallpaper")
        if updated != text:
            atomic_write(path, updated.encode("utf-8"))
    except (ConfigError, OSError):
        pass                                    # appearance.toml is read after it and wins anyway


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
