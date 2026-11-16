"""boswas-preset: list and apply the Boswas OS visual presets.

A preset is one of the ten looks of desktop/presets. Applying one sets
light or dark for the Boswas desktop (shell, window frames, lock screen),
the wallpaper, and the colour scheme of KDE applications the user may have
installed. The Boswas Control Center calls this tool; its interface is a
contract:

  boswas-preset [--json] list
      {"presets": [{"id", "name", "description", "variant", "accent",
                    "preview"}, ...], "current": "<id>" | null}
  boswas-preset [--json] current
      the current preset id (nothing if none is applied); --json:
      {"current": ...}
  boswas-preset [--json] apply <id>
      applies the preset for the signed-in user; --json: {"applied": "<id>"}
  boswas-preset [--json] reset
      returns to the default look, Boswas Prism, which is no preset;
      --json: {"reset": true, "default": "Boswas Prism"}

apply changes three files of the user, in this order, and then asks the
running compositor to re-read its configuration:

  <config>/boswas/appearance.toml   light or dark and the wallpaper, read by
                                    boswas-compositor (step "appearance")
  <config>/kdeglobals               the colour groups of the preset's scheme,
                                    read by KDE applications (step "colours")
  <config>/boswasrc                 [Preset] Current=<id>, what `current`
                                    reports (step "record")

<config> is $XDG_CONFIG_HOME when that is an absolute path, else ~/.config.
The new content of all three is prepared before the first of them is
written, so a file that cannot be used stops the command while nothing has
changed yet. Each file is replaced atomically (a new file with mode 0600 in
the same directory, synced, then renamed). A target that is a symbolic link
or not a regular file is refused, and so is a symbolic link in the place of
<config>/boswas: nothing is read or written through a link below <config>.

Exit status: 0 success, 1 a step failed (or the presets file is unreadable),
2 unknown preset id, 3 usage error, 4 run as root.

This tool starts no other program. Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import secrets
import socket
import stat
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO

PRESETS_FILE = "/usr/share/boswas/presets/presets.json"
WALLPAPER_DIR = "/usr/share/wallpapers"
PREVIEW_DIR = "/usr/share/boswas/presets/previews"
COLOR_SCHEME_DIR = "/usr/share/color-schemes"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNKNOWN = 2
EXIT_USAGE = 3
EXIT_ROOT = 4

# What a preset id and label may be: both become parts of file names.
PRESET_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PRESET_LABEL = re.compile(r"^[A-Za-z0-9]{1,32}$")
ACCENT = re.compile(r"^#[0-9A-Fa-f]{6}$")
# The largest file this tool reads to rewrite it (kdeglobals, boswasrc) or to
# copy colours from (a colour scheme).
MAX_FILE = 1024 * 1024

# The look the desktop starts with and `reset` returns to: Boswas UI "Prism"
# (desktop/ui), which is no preset of its own.
DEFAULT_LOOK = "Boswas Prism"
DEFAULT_SCHEME = "BoswasPrism"

# The user's files, as (directories below <config>, file name).
APPEARANCE = (("boswas",), "appearance.toml")
KDEGLOBALS = ((), "kdeglobals")
RECORD = ((), "boswasrc")
RECORD_GROUP, RECORD_KEY = "Preset", "Current"


class UsageError(Exception):
    pass


class PresetError(Exception):
    def __init__(self, message: str, code: int, step: str | None = None):
        super().__init__(message)
        self.code = code
        self.step = step


class Refused(OSError):
    """A file of the user that this tool will not read, replace or remove."""


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------

def presets_file(env: Mapping[str, str]) -> Path:
    return Path(env.get("BOSWAS_PRESETS_FILE") or PRESETS_FILE)


def _checked(preset: object) -> dict:
    """A preset of the presets file, with everything this tool uses validated."""
    if not isinstance(preset, dict):
        raise TypeError("a preset is not an object")
    for key in ("id", "label", "name", "description", "variant", "palette"):
        if key not in preset:
            raise KeyError(key)
    if not isinstance(preset["id"], str) or not PRESET_ID.fullmatch(preset["id"]):
        raise ValueError(f"invalid preset id {preset['id']!r}")
    if not isinstance(preset["label"], str) or not PRESET_LABEL.fullmatch(preset["label"]):
        raise ValueError(f"invalid label of preset {preset['id']}")
    if preset["variant"] not in ("dark", "light"):
        raise ValueError(f"the variant of preset {preset['id']} is neither dark nor light")
    for key in ("name", "description"):
        if not isinstance(preset[key], str) or any(ord(character) < 32 for character in preset[key]):
            raise ValueError(f"invalid {key} of preset {preset['id']}")
    accent = preset["palette"]["accent"]
    if not isinstance(accent, str) or not ACCENT.fullmatch(accent):
        raise ValueError(f"invalid accent colour of preset {preset['id']}")
    return preset


def load_presets(env: Mapping[str, str]) -> list[dict]:
    path = presets_file(env)
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        presets = data["presets"]
        if not isinstance(presets, list):
            raise TypeError("\"presets\" is not a list")
        presets = [_checked(preset) for preset in presets]
        if len({preset["id"] for preset in presets}) != len(presets):
            raise ValueError("a preset id is used twice")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PresetError(f"cannot read the presets file {path}: {error}", EXIT_FAILED) from error
    return presets


def find(presets: list[dict], preset_id: str) -> dict:
    for preset in presets:
        if preset["id"] == preset_id:
            return preset
    known = ", ".join(p["id"] for p in presets)
    raise PresetError(f"unknown preset '{preset_id}' (known presets: {known})", EXIT_UNKNOWN)


def wallpaper_package(preset: dict) -> str:
    return f"{WALLPAPER_DIR}/Boswas-{preset['label']}"


def wallpaper_image(preset: dict) -> str:
    """The wallpaper picture that matches the preset's light or dark variant."""
    images = "images_dark" if preset["variant"] == "dark" else "images"
    return f"{wallpaper_package(preset)}/contents/{images}/3840x2160.png"


def preview_image(preset: dict) -> str:
    return f"{PREVIEW_DIR}/{preset['id']}.png"


def scheme_name(preset: dict) -> str:
    return f"Boswas{preset['label']}"


def color_scheme_file(env: Mapping[str, str], name: str) -> Path:
    return Path(env.get("BOSWAS_COLOR_SCHEMES") or COLOR_SCHEME_DIR) / f"{name}.colors"


def summary(preset: dict) -> dict:
    return {
        "id": preset["id"],
        "name": preset["name"],
        "description": preset["description"],
        "variant": preset["variant"],
        "accent": preset["palette"]["accent"],
        "preview": preview_image(preset),
    }


# --------------------------------------------------------------------------
# The user's configuration files
# --------------------------------------------------------------------------

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


def config_home(env: Mapping[str, str]) -> Path:
    """<config>: $XDG_CONFIG_HOME when it is an absolute path, else ~/.config."""
    base = env.get("XDG_CONFIG_HOME") or ""
    if os.path.isabs(base) and "\0" not in base:
        return Path(os.path.normpath(base))
    return Path(env.get("HOME") or os.path.expanduser("~")) / ".config"


def user_file(env: Mapping[str, str], target: tuple[tuple[str, ...], str]) -> Path:
    directories, name = target
    return config_home(env).joinpath(*directories, name)


def _kind(name: str, dir_fd: int) -> str:
    """missing, link, dir, file or other: what `name` in the open directory is (a link is not followed)."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(info.st_mode):
        return "link"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    return "file" if stat.S_ISREG(info.st_mode) else "other"


def _open_directory(env: Mapping[str, str], directories: tuple[str, ...], create: bool) -> int | None:
    """An open descriptor of <config>/<directories...>; None when it does not
    exist and `create` is not set. No directory below <config> is reached
    through a symbolic link; missing ones are created with mode 0700."""
    base = config_home(env)
    try:
        if create:
            os.makedirs(base, mode=0o700, exist_ok=True)
        current = os.open(base, _DIR_FLAGS)
    except FileNotFoundError:
        if create:
            raise
        return None
    shown = base
    try:
        for part in directories:
            shown = shown / part
            kind = _kind(part, current)
            if kind == "missing":
                if not create:
                    os.close(current)
                    return None
                os.mkdir(part, mode=0o700, dir_fd=current)
            elif kind == "link":
                raise Refused(f"{shown} is a symbolic link; refusing to follow it")
            elif kind != "dir":
                raise Refused(f"{shown} is not a folder")
            following = os.open(part, _DIR_FLAGS | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = following
    except BaseException:
        os.close(current)
        raise
    return current


def _refuse_unless_replaceable(kind: str, shown: Path) -> None:
    if kind == "link":
        raise Refused(f"{shown} is a symbolic link; refusing to follow it")
    if kind not in ("missing", "file"):
        raise Refused(f"{shown} is not a regular file; it was not changed")


def check_user_file(env: Mapping[str, str], target: tuple[tuple[str, ...], str]) -> None:
    """Raise OSError now if the file could not be replaced or removed later."""
    directories, name = target
    dir_fd = _open_directory(env, directories, create=False)
    if dir_fd is None:
        return
    try:
        _refuse_unless_replaceable(_kind(name, dir_fd), user_file(env, target))
    finally:
        os.close(dir_fd)


def _decode(data: bytes, shown: Path) -> str:
    if len(data) > MAX_FILE:
        raise Refused(f"{shown} is larger than {MAX_FILE // 1024} KiB")
    # Bytes that are not UTF-8 come back out as they went in.
    return data.decode("utf-8", "surrogateescape")


def read_user_file(env: Mapping[str, str], target: tuple[tuple[str, ...], str]) -> str | None:
    """The text of a file of the user; None when it does not exist. A
    symbolic link, anything that is not a regular file and a file larger
    than MAX_FILE are refused (OSError)."""
    directories, name = target
    shown = user_file(env, target)
    dir_fd = _open_directory(env, directories, create=False)
    if dir_fd is None:
        return None
    try:
        _refuse_unless_replaceable(_kind(name, dir_fd), shown)
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:          # replaced by a link since the check above
                raise Refused(f"{shown} is a symbolic link; refusing to follow it") from None
            raise
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise Refused(f"{shown} is not a regular file; it was not changed")
            return _decode(handle.read(MAX_FILE + 1), shown)
    finally:
        os.close(dir_fd)


def replace_user_file(env: Mapping[str, str], target: tuple[tuple[str, ...], str], text: str) -> None:
    """Replace a file of the user with `text`: a new file (mode 0600) in the
    same directory, written and synced, then renamed over the old one. The
    old file stays as it was when anything fails."""
    directories, name = target
    data = text.encode("utf-8", "surrogateescape")
    dir_fd = _open_directory(env, directories, create=True)
    try:
        _refuse_unless_replaceable(_kind(name, dir_fd), user_file(env, target))
        temporary = f".{name}.{secrets.token_hex(8)}.tmp"
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=dir_fd)
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view):]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        try:
            os.fsync(dir_fd)                        # the rename itself; not every file system can
        except OSError:
            pass
    finally:
        os.close(dir_fd)


def remove_user_file(env: Mapping[str, str], target: tuple[tuple[str, ...], str]) -> None:
    """Delete a file of the user if it exists (the same rules as replace_user_file)."""
    directories, name = target
    dir_fd = _open_directory(env, directories, create=False)
    if dir_fd is None:
        return
    try:
        kind = _kind(name, dir_fd)
        if kind == "missing":
            return
        _refuse_unless_replaceable(kind, user_file(env, target))
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    finally:
        os.close(dir_fd)


# --------------------------------------------------------------------------
# What the files contain
# --------------------------------------------------------------------------

def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def appearance_text(preset: dict) -> str:
    """<config>/boswas/appearance.toml: light or dark and the wallpaper,
    read by boswas-compositor and passed on to the shell. The file is
    rewritten as a whole."""
    return ("# Written by boswas-preset; changes made here are replaced by the next preset.\n"
            "[appearance]\n"
            f"scheme = {_toml_string(preset['variant'])}\n"
            f"wallpaper = {_toml_string(wallpaper_image(preset))}\n")


def _ini_groups(text: str) -> list[tuple[str | None, str, list[str]]]:
    """Split KConfig text into its groups, in order: (the header without the
    blanks around it, the header line as it is written, the lines of the
    group). The lines before the first header come first, under None."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()                                 # the end of the last line
    groups: list[tuple[str | None, str, list[str]]] = [(None, "", [])]
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            groups.append((stripped, line, []))
        else:
            groups[-1][2].append(line)
    return groups


def merge_colors(kdeglobals: str, scheme: str, name: str) -> str:
    """kdeglobals with the colour groups of a scheme: what KDE applications
    read their colours from. Every other group is kept as it is."""
    def is_color(header: str | None) -> bool:
        return header is not None and (header.startswith("[Colors:") or header == "[WM]")

    kept = [(header, body) for header, _line, body in _ini_groups(kdeglobals) if not is_color(header)]
    colors = [(header, body) for header, _line, body in _ini_groups(scheme) if is_color(header)]
    for index, (header, body) in enumerate(kept):
        if header == "[General]":
            kept[index] = (header, [line for line in body if _entry_key(line) != "ColorScheme"])
            break
    else:
        kept.append(("[General]", []))
        index = len(kept) - 1
    kept[index][1].insert(0, f"ColorScheme={name}")
    out: list[str] = []
    for header, body in kept + colors:
        while body and not body[-1].strip():
            body = body[:-1]
        if header is None and not body:
            continue
        if header is not None:
            out.append(header)
        out.extend(body)
        out.append("")
    return "\n".join(out)


_MARKERS = re.compile(r"(\[[^\]]*\])+$")


def _entry_key(line: str) -> str | None:
    """The key of a KConfig entry line, without markers such as [$i]; None
    for an empty line, a comment and a group header."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "[")):
        return None
    return _MARKERS.sub("", stripped.partition("=")[0].strip())


def edit_kconfig(text: str, group: str, key: str, value: str | None) -> str:
    """KConfig text with `key` of `[group]` set to `value`, or removed when
    `value` is None. Every other line stays as it is: other groups, other
    keys, comments, empty lines. A `[group]` left without any line is
    removed with its header."""
    header = f"[{group}]"
    groups = _ini_groups(text)
    first = None
    for index, (name, _line, body) in enumerate(groups):
        if name == header:
            body[:] = [line for line in body if _entry_key(line) != key]
            if first is None:
                first = index
    if value is None:
        groups = [entry for entry in groups if entry[0] != header or any(line.strip() for line in entry[2])]
    elif first is None:
        name, _line, body = groups[-1]
        if (name is not None or body) and (not body or body[-1].strip()):
            body.append("")
        groups.append((header, header, [f"{key}={value}"]))
    else:
        body = groups[first][2]
        position = len(body)
        while position > 0 and not body[position - 1].strip():
            position -= 1
        body.insert(position, f"{key}={value}")
    out: list[str] = []
    for name, line, body in groups:
        if name is not None:
            out.append(line)
        out.extend(body)
    return "\n".join(out) + "\n" if out else ""


def read_kconfig(path: Path, group: str, key: str) -> str | None:
    """Read one key of a KConfig (INI) file; None if the file or key is missing."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    current = None
    value = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        if current == group and "=" in line:
            name, _, data = line.partition("=")
            name = name.strip()
            if name.endswith("[$i]"):
                name = name[:-4]
            if name == key:
                value = data.strip()
    return value


def current_preset(env: Mapping[str, str], presets: list[dict]) -> str | None:
    """The preset last applied with this tool: [Preset] Current in
    <config>/boswasrc (the file earlier versions wrote with KDE's
    configuration tool has the same format), if it names a known preset."""
    recorded = read_kconfig(user_file(env, RECORD), RECORD_GROUP, RECORD_KEY)
    return recorded if recorded in {p["id"] for p in presets} else None


# --------------------------------------------------------------------------
# Applying a preset, returning to the default look
# --------------------------------------------------------------------------
#
# A step is (name, prepare). prepare() reads and checks what the step needs
# and returns the function that writes; it changes nothing itself. Every
# step is prepared before the first one writes.

Step = tuple[str, Callable[[], Callable[[], None]]]


def _read_scheme(env: Mapping[str, str], name: str) -> str:
    path = color_scheme_file(env, name)
    with path.open("rb") as handle:
        return _decode(handle.read(MAX_FILE + 1), path)


def _colours_step(env: Mapping[str, str], name: str) -> Step:
    def prepare() -> Callable[[], None]:
        scheme = _read_scheme(env, name)
        merged = merge_colors(read_user_file(env, KDEGLOBALS) or "", scheme, name)
        return lambda: replace_user_file(env, KDEGLOBALS, merged)
    return "colours", prepare


def apply_steps(env: Mapping[str, str], preset: dict) -> list[Step]:
    def appearance() -> Callable[[], None]:
        check_user_file(env, APPEARANCE)
        text = appearance_text(preset)
        return lambda: replace_user_file(env, APPEARANCE, text)

    def record() -> Callable[[], None]:
        text = edit_kconfig(read_user_file(env, RECORD) or "", RECORD_GROUP, RECORD_KEY, preset["id"])
        return lambda: replace_user_file(env, RECORD, text)

    return [("appearance", appearance), _colours_step(env, scheme_name(preset)), ("record", record)]


def reset_steps(env: Mapping[str, str]) -> list[Step]:
    """Back to the default look: no appearance file (the compositor and the
    shell fall back to Prism), the Prism colours for KDE applications, and
    no recorded preset."""
    def appearance() -> Callable[[], None]:
        check_user_file(env, APPEARANCE)
        return lambda: remove_user_file(env, APPEARANCE)

    def record() -> Callable[[], None]:
        old = read_user_file(env, RECORD)
        new = edit_kconfig(old or "", RECORD_GROUP, RECORD_KEY, None)
        if old is None or new == old:
            return lambda: None                     # nothing is recorded
        if not new.strip():
            return lambda: remove_user_file(env, RECORD)
        return lambda: replace_user_file(env, RECORD, new)

    return [("appearance", appearance), _colours_step(env, DEFAULT_SCHEME), ("record", record)]


def run_steps(steps: list[Step]) -> None:
    def failed(step: str, error: OSError) -> PresetError:
        return PresetError(f"step '{step}' failed: {error}", EXIT_FAILED, step)

    writes = []
    for step, prepare in steps:
        try:
            writes.append((step, prepare()))
        except OSError as error:
            raise failed(step, error) from None
    for step, write in writes:
        try:
            write()
        except OSError as error:
            raise failed(step, error) from None


def reload_compositor(env: Mapping[str, str]) -> bool:
    """Ask the running compositor to re-read its configuration. Without a
    running compositor the new look applies at the next login."""
    runtime, display = env.get("XDG_RUNTIME_DIR"), env.get("WAYLAND_DISPLAY")
    if not runtime or not display or "/" in display:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(Path(runtime) / f"boswas-{display}.sock"))
            connection.sendall(b'{"cmd": "reload-config"}\n')
            return b'"ok":true' in connection.recv(4096).replace(b" ", b"")
    except OSError:
        return False


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

class Parser(argparse.ArgumentParser):
    def error(self, message: str):  # usage errors exit 3 (2 means "unknown preset")
        raise UsageError(message)


def build_parser() -> Parser:
    common = Parser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print machine-readable JSON")
    parser = Parser(prog="boswas-preset", parents=[common],
                    description="List and apply the Boswas OS visual presets.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=Parser)
    commands.add_parser("list", parents=[common], help="list the presets")
    commands.add_parser("current", parents=[common], help="print the current preset")
    apply = commands.add_parser("apply", parents=[common], help="apply a preset")
    apply.add_argument("preset", metavar="ID", help="preset id (see 'list')")
    commands.add_parser("reset", parents=[common], help="return to the default look")
    return parser


def emit(out: TextIO, data) -> None:
    out.write(json.dumps(data, indent=2) + "\n")


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         geteuid: Callable[[], int] | None = None, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    env = os.environ if env is None else env
    geteuid = geteuid or os.geteuid
    args_list = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args_list

    try:
        args = build_parser().parse_args(args_list)
        if not args.command:
            raise UsageError("a command is required (list, current, apply, reset)")
        as_json = bool(getattr(args, "json", False))
        presets = load_presets(env)

        if args.command == "list":
            current = current_preset(env, presets)
            if as_json:
                emit(out, {"presets": [summary(p) for p in presets], "current": current})
            else:
                for p in presets:
                    marker = "*" if p["id"] == current else " "
                    out.write(f"{marker} {p['id']:<10} {p['name']:<18} {p['variant']:<6} {p['description']}\n")
            return EXIT_OK

        if args.command == "current":
            current = current_preset(env, presets)
            if as_json:
                emit(out, {"current": current})
            elif current:
                out.write(current + "\n")
            return EXIT_OK

        # apply, reset
        if geteuid() == 0:
            raise PresetError("presets are per-user settings: run boswas-preset as the logged-in user, "
                              "not as root", EXIT_ROOT)
        if args.command == "reset":
            run_steps(reset_steps(env))
            reload_compositor(env)
            if as_json:
                emit(out, {"reset": True, "default": DEFAULT_LOOK})
            else:
                out.write(f"Reset to {DEFAULT_LOOK}.\n")
            return EXIT_OK
        preset = find(presets, args.preset)
        run_steps(apply_steps(env, preset))
        reload_compositor(env)
        if as_json:
            emit(out, {"applied": preset["id"]})
        else:
            out.write(f"Applied {preset['name']}.\n")
        return EXIT_OK

    except UsageError as error:
        err.write(f"boswas-preset: {error}\n")
        err.write("usage: boswas-preset [--json] {list,current,apply ID,reset}\n")
        if as_json:
            emit(out, {"error": str(error), "code": EXIT_USAGE})
        return EXIT_USAGE
    except PresetError as error:
        err.write(f"boswas-preset: {error}\n")
        if as_json:
            data = {"error": str(error), "code": error.code}
            if error.step:
                data["step"] = error.step
            emit(out, data)
        return error.code


if __name__ == "__main__":
    sys.exit(main())
