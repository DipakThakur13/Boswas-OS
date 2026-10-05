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
            raise UsageError("a command is required (list, current, apply)")
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

        # apply
        if geteuid() == 0:
            raise PresetError("presets are per-user settings: run boswas-preset as the logged-in user, "
                              "not as root", EXIT_ROOT)
        preset = find(presets, args.preset)
        run_apply(preset, args.layout, runner)
        if as_json:
            emit(out, {"applied": preset["id"], "layout": bool(args.layout)})
        else:
            out.write(f"Applied {preset['name']}.\n")
        return EXIT_OK

    except UsageError as error:
        err.write(f"boswas-preset: {error}\n")
        err.write("usage: boswas-preset [--json] {list,current,apply ID [--layout]}\n")
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
