"""boswas-preset: list and apply the Boswas OS visual presets.

A preset bundles a Plasma Global Theme (com.boswas.<id>: colour scheme, icons,
Plasma style, window decoration, panel layout), a wallpaper, a lock screen
background and a Konsole profile. The Boswas Control Center calls this tool;
its interface is a contract:

  boswas-preset [--json] list
      {"presets": [{"id", "name", "description", "variant", "accent",
                    "preview"}, ...], "current": "<id>" | null}
  boswas-preset [--json] current
      the current preset id (nothing if unknown); --json: {"current": ...}
  boswas-preset [--json] apply <id> [--layout]
      applies the preset for the logged-in user; --layout also replaces the
      user's panels with the preset's layout; --json: {"applied", "layout"}

Exit status: 0 success, 1 a step failed (or the presets file is unreadable),
2 unknown preset id, 3 usage error, 4 run as root.

Every external program is run with a fixed argument list, never through a
shell. Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO

PRESETS_FILE = "/usr/share/boswas/presets/presets.json"
WALLPAPER_DIR = "/usr/share/wallpapers"
LOCK_DIR = "/usr/share/boswas/presets/lock"
PREVIEW_DIR = "/usr/share/boswas/presets/previews"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNKNOWN = 2
EXIT_USAGE = 3
EXIT_ROOT = 4

Runner = Callable[[Sequence[str]], int]


class UsageError(Exception):
    pass


class PresetError(Exception):
    def __init__(self, message: str, code: int, step: str | None = None):
        super().__init__(message)
        self.code = code
        self.step = step


# --------------------------------------------------------------------------
# Running programs
# --------------------------------------------------------------------------

def default_runner(argv: Sequence[str]) -> int:
    """Run argv (a list, never a shell string); its output goes to stderr so
    that stdout stays clean for --json."""
    try:
        completed = subprocess.run(list(argv), shell=False, check=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        sys.stderr.write(f"boswas-preset: {argv[0]}: command not found\n")
        return 127
    except OSError as error:
        sys.stderr.write(f"boswas-preset: {argv[0]}: {error}\n")
        return 126
    if completed.stdout:
        sys.stderr.write(completed.stdout.decode("utf-8", "replace"))
    return completed.returncode


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------

def presets_file(env: Mapping[str, str]) -> Path:
    return Path(env.get("BOSWAS_PRESETS_FILE") or PRESETS_FILE)


def load_presets(env: Mapping[str, str]) -> list[dict]:
    path = presets_file(env)
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        presets = data["presets"]
        for preset in presets:
            for key in ("id", "label", "name", "description", "variant", "palette"):
                if key not in preset:
                    raise KeyError(key)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PresetError(f"cannot read the presets file {path}: {error}", EXIT_FAILED) from error
    return presets


def find(presets: list[dict], preset_id: str) -> dict:
    for preset in presets:
        if preset["id"] == preset_id:
            return preset
    known = ", ".join(p["id"] for p in presets)
    raise PresetError(f"unknown preset '{preset_id}' (known presets: {known})", EXIT_UNKNOWN)


def look_and_feel(preset: dict) -> str:
    return "com.boswas." + preset["id"]


def wallpaper_package(preset: dict) -> str:
    return f"{WALLPAPER_DIR}/Boswas-{preset['label']}"


def lock_image(preset: dict) -> str:
    return f"{LOCK_DIR}/{preset['id']}.png"


def preview_image(preset: dict) -> str:
    return f"{PREVIEW_DIR}/{preset['id']}.png"


def konsole_profile(preset: dict) -> str:
    return f"Boswas {preset['label']}.profile"


def summary(preset: dict) -> dict:
    return {
        "id": preset["id"],
        "name": preset["name"],
        "description": preset["description"],
        "variant": preset["variant"],
        "accent": preset["palette"]["accent"],
        "preview": preview_image(preset),
    }


def apply_steps(preset: dict, layout: bool) -> list[tuple[str, list[str]]]:
    """The commands that apply a preset, in order, as (step name, argv)."""
    lnf = ["plasma-apply-lookandfeel", "-a", look_and_feel(preset)]
    if layout:
        lnf.append("--resetLayout")
    return [
        # Colour scheme, icons, Plasma style, cursor, window decoration (and,
        # with --resetLayout, the panels). It does not set the wallpaper.
        ("look-and-feel", lnf),
        # The wallpaper package: Plasma shows images_dark/ with a dark colour
        # scheme and images/ with a light one.
        ("wallpaper", ["plasma-apply-wallpaperimage", wallpaper_package(preset)]),
        ("lock-screen", ["kwriteconfig6", "--file", "kscreenlockerrc",
                         "--group", "Greeter", "--group", "Wallpaper", "--group", "org.kde.image",
                         "--group", "General", "--key", "Image", "file://" + lock_image(preset)]),
        ("terminal", ["kwriteconfig6", "--file", "konsolerc", "--group", "Desktop Entry",
                      "--key", "DefaultProfile", konsole_profile(preset)]),
        ("record", ["kwriteconfig6", "--file", "boswasrc", "--group", "Preset",
                    "--key", "Current", preset["id"]]),
    ]


# --------------------------------------------------------------------------
# The native Boswas desktop
# --------------------------------------------------------------------------

def is_native_desktop(env: Mapping[str, str]) -> bool:
    """Whether this is the native Boswas desktop (boswas-compositor and
    boswas-shell) rather than a KDE Plasma session."""
    return "Boswas" in (env.get("XDG_CURRENT_DESKTOP") or "").split(":")


def wallpaper_image(preset: dict) -> str:
    """The wallpaper picture that matches the preset's light or dark variant."""
    images = "images_dark" if preset["variant"] == "dark" else "images"
    return f"{wallpaper_package(preset)}/contents/{images}/3840x2160.png"


def color_scheme_file(env: Mapping[str, str], preset: dict) -> Path:
    return Path(env.get("BOSWAS_COLOR_SCHEMES") or "/usr/share/color-schemes") / f"Boswas{preset['label']}.colors"


def native_steps(preset: dict) -> list[tuple[str, list[str]]]:
    """The commands of a native apply; the appearance file and the colour
    scheme are written by this tool itself (write_appearance, write_colors)."""
    return [
        ("terminal", ["kwriteconfig6", "--file", "konsolerc", "--group", "Desktop Entry",
                      "--key", "DefaultProfile", konsole_profile(preset)]),
        ("record", ["kwriteconfig6", "--file", "boswasrc", "--group", "Preset",
                    "--key", "Current", preset["id"]]),
    ]


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".boswas-preset.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_appearance(env: Mapping[str, str], preset: dict) -> None:
    """~/.config/boswas/appearance.toml: light or dark and the wallpaper,
    read by boswas-compositor and passed on to the shell. The file belongs
    to this tool and is rewritten as a whole."""
    text = ("# Written by boswas-preset; changes made here are replaced by the next preset.\n"
            "[appearance]\n"
            f"scheme = {_toml_string(preset['variant'])}\n"
            f"wallpaper = {_toml_string(wallpaper_image(preset))}\n")
    _write_atomically(config_home(env) / "boswas" / "appearance.toml", text)


def _ini_groups(text: str) -> list[tuple[str | None, list[str]]]:
    """Split KConfig text into (group header line, body lines), in order."""
    groups: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in text.splitlines():
        if line.startswith("[") and line.rstrip().endswith("]"):
            groups.append((line.rstrip(), []))
        else:
            groups[-1][1].append(line)
    return groups


def merge_colors(kdeglobals: str, scheme: str, scheme_name: str) -> str:
    """kdeglobals with the colour groups of a scheme: what KDE applications
    read their colours from. Every other group is kept as it is."""
    def is_color(header: str | None) -> bool:
        return header is not None and (header.startswith("[Colors:") or header == "[WM]")

    kept = [(header, body) for header, body in _ini_groups(kdeglobals) if not is_color(header)]
    colors = [(header, body) for header, body in _ini_groups(scheme) if is_color(header)]
    for index, (header, body) in enumerate(kept):
        if header == "[General]":
            kept[index] = (header, [line for line in body if line.split("=", 1)[0].strip() != "ColorScheme"])
            break
    else:
        kept.append(("[General]", []))
        index = len(kept) - 1
    kept[index][1].insert(0, f"ColorScheme={scheme_name}")
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


def write_colors(env: Mapping[str, str], preset: dict) -> None:
    scheme = color_scheme_file(env, preset).read_text(encoding="utf-8")
    target = config_home(env) / "kdeglobals"
    try:
        current = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    _write_atomically(target, merge_colors(current, scheme, f"Boswas{preset['label']}"))


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


# The look the native desktop starts with: Boswas UI v2 "Prism"
# (desktop/ui), which is no preset of its own.
DEFAULT_SCHEME = "BoswasPrism"
DEFAULT_KONSOLE_PROFILE = "Boswas Prism.profile"
# On a Plasma session the default is the Horizon preset with its layout.
PLASMA_DEFAULT_PRESET = "horizon"


def reset_steps() -> list[tuple[str, list[str]]]:
    """The commands of a native reset; the appearance file and the colours
    are handled by this tool itself."""
    return [
        ("terminal", ["kwriteconfig6", "--file", "konsolerc", "--group", "Desktop Entry",
                      "--key", "DefaultProfile", DEFAULT_KONSOLE_PROFILE]),
        ("record", ["kwriteconfig6", "--file", "boswasrc", "--group", "Preset", "--key", "Current", "--delete"]),
    ]


def run_native_reset(env: Mapping[str, str], runner: Runner) -> None:
    """Back to the default look: no preset file (the compositor and the
    shell fall back to Prism), the Prism colours for KDE applications and
    the Prism terminal profile."""
    try:
        (config_home(env) / "boswas" / "appearance.toml").unlink(missing_ok=True)
    except OSError as error:
        raise PresetError(f"step 'appearance' failed: {error}", EXIT_FAILED, "appearance") from None
    try:
        directory = Path(env.get("BOSWAS_COLOR_SCHEMES") or "/usr/share/color-schemes")
        scheme = (directory / f"{DEFAULT_SCHEME}.colors").read_text(encoding="utf-8")
        target = config_home(env) / "kdeglobals"
        try:
            current = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = ""
        _write_atomically(target, merge_colors(current, scheme, DEFAULT_SCHEME))
    except OSError as error:
        raise PresetError(f"step 'colours' failed: {error}", EXIT_FAILED, "colours") from None
    for step, argv in reset_steps():
        status = runner(argv)
        if status != 0:
            raise PresetError(f"step '{step}' failed: {' '.join(argv)} exited with status {status}",
                              EXIT_FAILED, step)
    reload_compositor(env)


def run_native_apply(preset: dict, env: Mapping[str, str], runner: Runner) -> None:
    for step, action in (("appearance", write_appearance), ("colours", write_colors)):
        try:
            action(env, preset)
        except OSError as error:
            raise PresetError(f"step '{step}' failed: {error}", EXIT_FAILED, step) from None
    for step, argv in native_steps(preset):
        status = runner(argv)
        if status != 0:
            raise PresetError(f"step '{step}' failed: {' '.join(argv)} exited with status {status}",
                              EXIT_FAILED, step)
    reload_compositor(env)


# --------------------------------------------------------------------------
# Current preset
# --------------------------------------------------------------------------

def config_home(env: Mapping[str, str]) -> Path:
    if env.get("XDG_CONFIG_HOME"):
        return Path(env["XDG_CONFIG_HOME"])
    return Path(env.get("HOME") or os.path.expanduser("~")) / ".config"


def config_dirs(env: Mapping[str, str]) -> list[Path]:
    return [Path(d) for d in (env.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if d]


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
    """The preset last applied with this tool (~/.config/boswasrc), or else
    the preset whose Global Theme is active (kdeglobals, user then system)."""
    ids = {p["id"] for p in presets}
    recorded = read_kconfig(config_home(env) / "boswasrc", "Preset", "Current")
    if recorded in ids:
        return recorded
    for directory in [config_home(env)] + config_dirs(env):
        package = read_kconfig(directory / "kdeglobals", "KDE", "LookAndFeelPackage")
        if package:
            if package.startswith("com.boswas.") and package[len("com.boswas."):] in ids:
                return package[len("com.boswas."):]
            return None
    return None


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
    apply.add_argument("--layout", action="store_true",
                       help="also apply the preset's panel layout (replaces your panels)")
    commands.add_parser("reset", parents=[common], help="return to the default look")
    return parser


def emit(out: TextIO, data) -> None:
    out.write(json.dumps(data, indent=2) + "\n")


def run_apply(preset: dict, layout: bool, runner: Runner) -> None:
    for step, argv in apply_steps(preset, layout):
        status = runner(argv)
        if status != 0:
            raise PresetError(f"step '{step}' failed: {' '.join(argv)} exited with status {status}",
                              EXIT_FAILED, step)


def main(argv: Sequence[str] | None = None, *, runner: Runner | None = None,
         env: Mapping[str, str] | None = None, geteuid: Callable[[], int] | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    env = os.environ if env is None else env
    runner = runner or default_runner
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
        native = is_native_desktop(env)
        if args.command == "reset":
            if native:
                run_native_reset(env, runner)
                name = "Boswas Prism"
            else:
                # A Plasma session: the default is the Horizon preset and its panels.
                preset = find(presets, PLASMA_DEFAULT_PRESET)
                run_apply(preset, True, runner)
                name = preset["name"]
            if as_json:
                emit(out, {"reset": True, "default": name})
            else:
                out.write(f"Reset to {name}.\n")
            return EXIT_OK
        preset = find(presets, args.preset)
        if native:
            # The native desktop has one panel layout; --layout has nothing to reset.
            run_native_apply(preset, env, runner)
        else:
            run_apply(preset, args.layout, runner)
        if as_json:
            emit(out, {"applied": preset["id"], "layout": bool(args.layout) and not native})
        else:
            out.write(f"Applied {preset['name']}.\n")
        return EXIT_OK

    except UsageError as error:
        err.write(f"boswas-preset: {error}\n")
        err.write("usage: boswas-preset [--json] {list,current,apply ID [--layout],reset}\n")
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
