"""boswas-android: the Android runtime manager of Boswas OS. See boswas-android(1).

Every command accepts --json (anywhere on the command line); JSON documents
always carry "schema": "boswas-android/1". Errors print
{"schema", "error": {"code", "message", "problems"}} with the exit codes of
errors.py. The runtime and service commands stream boswas-sysops's JSON Lines.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading

from . import JSON_SCHEMA, __version__, apk, checks, config, googleplay, runtime
from .errors import EXIT_INTERNAL, EXIT_OK, AndroidError, Refused
from .runtime import Context

NOT_SAFE_NOTE = ("Inspection describes the file; it does not show that the app is safe. Android checks the "
                 "signature when it installs the app.")


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable JSON output")
    parser = argparse.ArgumentParser(prog="boswas-android", parents=[common],
                                     description="Manage the Android runtime (Waydroid) and Android apps on Boswas OS.")
    parser.add_argument("--version", action="version", version=f"boswas-android {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True
    sub.add_parser("status", parents=[common], help="runtime, compatibility, images, session, storage, network, GPU")
    sub.add_parser("check", parents=[common], help="can this computer run the Android runtime?")

    apk_p = sub.add_parser("apk", parents=[common], help="inspect or install an APK file")
    apk_sub = apk_p.add_subparsers(dest="action", metavar="ACTION")
    apk_sub.required = True
    for name, text in (("inspect", "describe an APK (never runs it)"), ("install", "inspect, then install")):
        p = apk_sub.add_parser(name, parents=[common], help=text)
        p.add_argument("file")

    app_p = sub.add_parser("app", parents=[common], help="installed Android apps")
    app_sub = app_p.add_subparsers(dest="action", metavar="ACTION")
    app_sub.required = True
    app_sub.add_parser("list", parents=[common], help="list installed apps")
    for name in ("launch", "remove"):
        p = app_sub.add_parser(name, parents=[common], help=f"{name} an app")
        p.add_argument("package")

    session_p = sub.add_parser("session", parents=[common], help="start or stop Android for this user")
    session_p.add_argument("action", choices=("start", "stop"))

    runtime_p = sub.add_parser("runtime", parents=[common], help="install, set up, update, repair or remove the runtime")
    runtime_p.add_argument("action", choices=("install", "init", "update", "repair", "remove"))
    runtime_p.add_argument("--purge-data", action="store_true", help="with remove: also delete the Android images")

    service_p = sub.add_parser("service", parents=[common], help="the Android container service")
    service_p.add_argument("action", choices=("start", "stop", "restart"))

    fdroid_p = sub.add_parser("fdroid", parents=[common], help="F-Droid, an independent app store (not Google Play)")
    fdroid_p.add_argument("action", choices=("install",))

    sub.add_parser("google-play", parents=[common], help="why Google Play is not available")
    logs_p = sub.add_parser("logs", parents=[common], help="Boswas and Waydroid logs")
    logs_p.add_argument("--lines", type=int, default=100)
    cleanup_p = sub.add_parser("cleanup", parents=[common],
                               help="after runtime removal: delete your Android data and leftover launcher entries")
    cleanup_p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return parser


# --- output -----------------------------------------------------------------------------------------

def _print_json(doc: dict) -> None:
    sys.stdout.write(json.dumps(doc, ensure_ascii=False, sort_keys=False) + "\n")
    sys.stdout.flush()


def _size(n) -> str:
    if n is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _text_checks(compat: dict) -> list[str]:
    lines = [f"Supported: {'yes' if compat['supported'] else 'no'}"]
    for check in compat["checks"]:
        lines.append(f"  {check['status']:<7} {check['title']}: {check['detail']}")
    return lines


def _text_status(doc: dict) -> str:
    rt = doc["runtime"]
    lines = [f"Android runtime: {'installed (Waydroid ' + str(rt['version']) + ')' if rt['installed'] else 'not installed'}",
             f"Set up: {'yes' if doc['initialized'] else 'no'}"]
    if doc["initialized"]:
        images = doc["images"]
        lines.append(f"Android images: {images['type'] or 'unknown'} (system {images['system'] or '?'}, "
                     f"vendor {images['vendor'] or '?'}, {_size(images['bytes'])})")
    lines.append(f"Container service: {'active' if doc['service']['active'] else 'not active'}")
    lines.append(f"Session: {'running' if doc['session']['running'] else 'stopped'}")
    lines.append(f"Your Android data: {_size(doc['storage']['user_bytes'])}")
    net = doc["network"]
    lines.append(f"Network: {net['interface']} {'present' if net['present'] else 'absent'}"
                 + (f" ({net['address']})" if net["address"] else ""))
    gpu = doc["gpu"]
    lines.append(f"Graphics: {gpu['acceleration']}" + (f" ({gpu['driver']})" if gpu["driver"] else ""))
    lines.append(f"Google Play: not available. {doc['google_play']['reason']}")
    lines.extend(_text_checks(doc["compatibility"]))
    return "\n".join(lines)


def _text_apk(doc: dict) -> str:
    lines = [f"File: {doc['path']} ({_size(doc['size'])}, SHA-256 {doc['sha256']})",
             f"Package: {doc['package'] or 'unknown'}",
             f"Name: {doc['label'] or '(not stated in the manifest)'}",
             f"Version: {doc['version_name'] or '?'} (code {doc['version_code'] if doc['version_code'] is not None else '?'})",
             f"Android: min SDK {doc['min_sdk'] or '?'}, target SDK {doc['target_sdk'] or '?'}",
             f"Native code: {', '.join(doc['abis']) if doc['abis'] else 'none (runs on any processor)'}",
             f"Split APK: {'yes (' + str(doc['split']['name']) + ')' if doc['split']['is_split'] else 'no'}"
             + ("; needs additional split APKs" if doc["split"]["requires_splits"] else ""),
             f"Signed with: {', '.join(doc['signing']['schemes']) or 'not signed'}"]
    for cert in doc["signing"]["certificates"]:
        lines.append(f"  certificate SHA-256 {cert['sha256']}")
    lines.append(f"Compatible: {'yes' if doc['compatible'] else 'no'}")
    for problem in doc["problems"]:
        lines.append(f"  - {problem}")
    lines.append(NOT_SAFE_NOTE)
    return "\n".join(lines)


class _EventPrinter:
    """Prints boswas-sysops events: JSON Lines as received, or readable text."""

    def __init__(self, as_json: bool):
        self.as_json = as_json

    def __call__(self, event: dict) -> None:
        if self.as_json:
            _print_json(event)
            return
        kind = event.get("type")
        if kind == "progress":
            percent = event.get("percent")
            prefix = f"[{event.get('phase', '…')}" + (f" {percent:.0f}%]" if isinstance(percent, (int, float)) else "]")
            print(f"{prefix} {event.get('message', '')}".rstrip(), flush=True)
        elif kind == "log":
            level = "warning: " if event.get("level") == "warning" else ""
            print(f"{level}{event.get('message', '')}", flush=True)
        elif kind == "result":
            if event.get("ok"):
                print(f"Done: {event.get('message', '')}", flush=True)
            else:
                print(f"Failed ({event.get('code')}): {event.get('message', '')}", file=sys.stderr, flush=True)


def _cancel_source() -> tuple[threading.Event, object]:
    """An event set when "cancel" arrives on stdin or on the first Ctrl+C (forwarded to boswas-sysops),
    and a function that restores the previous Ctrl+C handler."""
    event = threading.Event()

    def read_stdin():
        try:
            for line in sys.stdin:
                if line.strip() == "cancel":
                    event.set()
        except (OSError, ValueError):
            pass

    threading.Thread(target=read_stdin, name="cancel-reader", daemon=True).start()

    def on_sigint(_signum, _frame):
        if event.is_set():
            raise KeyboardInterrupt
        event.set()
        print("Cancel requested (only possible before anything is changed).", file=sys.stderr, flush=True)

    previous = None
    try:
        previous = signal.signal(signal.SIGINT, on_sigint)
    except ValueError:                       # not in the main thread
        pass

    def restore():
        if previous is not None:
            try:
                signal.signal(signal.SIGINT, previous)
            except ValueError:
                pass
    return event, restore


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# --- commands ---------------------------------------------------------------------------------------

def run(args, ctx: Context, as_json: bool) -> int:
    command = args.command
    if command == "status":
        doc = runtime.status(ctx)
        _print_json(doc) if as_json else print(_text_status(doc))
        return EXIT_OK
    if command == "check":
        cfg = config.waydroid_cfg(ctx.root)
        compat = checks.compatibility(ctx.root, ctx.host, cfg, runtime.initialized(ctx))
        _print_json({"schema": JSON_SCHEMA, "compatibility": compat}) if as_json else print("\n".join(_text_checks(compat)))
        return EXIT_OK
    if command == "apk":
        if args.action == "inspect":
            doc = apk.inspect(args.file, runtime.runtime_abis(ctx))
            _print_json({"schema": JSON_SCHEMA, "apk": doc}) if as_json else print(_text_apk(doc))
            return EXIT_OK
        doc = runtime.apk_install(ctx, args.file)
        _print_json(doc) if as_json else print(f"{doc['installed']['package']}: {doc['installed']['message']}")
        return EXIT_OK
    if command == "app":
        if args.action == "list":
            apps = runtime.app_list(ctx)
            if as_json:
                _print_json({"schema": JSON_SCHEMA, "apps": apps})
            else:
                print("\n".join(f"{a['package']}\t{a['name']}" for a in apps) or "No Android apps.")
            return EXIT_OK
        if args.action == "launch":
            doc = runtime.app_launch(ctx, args.package)
        else:
            doc = runtime.app_remove(ctx, args.package)
        _print_json(doc) if as_json else print(f"{args.package}: {'started' if args.action == 'launch' else 'removed'}")
        return EXIT_OK
    if command == "session":
        doc = runtime.session_start(ctx) if args.action == "start" else runtime.session_stop(ctx)
        if as_json:
            _print_json(doc)
        else:
            print(f"Android is {'running' if doc['session']['running'] else 'stopped'}"
                  + (" (it already was)." if doc["session"]["already"] else "."))
        return EXIT_OK
    if command in ("runtime", "service"):
        printer = _EventPrinter(as_json)
        cancel, restore = _cancel_source()
        try:
            if command == "runtime":
                outcome = runtime.runtime_operation(ctx, args.action, args.purge_data, printer, cancel)
            else:
                outcome = runtime.service_operation(ctx, args.action, printer, cancel)
        finally:
            restore()
        return outcome.exit_code
    if command == "fdroid":
        doc = runtime.fdroid_install(ctx)
        if as_json:
            _print_json(doc)
        else:
            print(f"F-Droid installed ({googleplay.ALTERNATIVE_LABELS['fdroid']}). {doc['installed']['message']}")
        return EXIT_OK
    if command == "google-play":
        doc = googleplay.detailed()
        if as_json:
            _print_json({"schema": JSON_SCHEMA, "google_play": doc})
        else:
            print(doc["reason"])
            for detail in doc["details"]:
                print(f"  - {detail}")
        return EXIT_OK
    if command == "logs":
        doc = runtime.logs(ctx, args.lines)
        if as_json:
            _print_json(doc)
        else:
            print(f"== Boswas Android log ({doc['boswas']['path']})")
            print("\n".join(doc["boswas"]["lines"]) or "(empty)")
            print(f"== Waydroid log ({doc['waydroid']['path']})")
            print("\n".join(doc["waydroid"]["lines"]) if doc["waydroid"]["readable"] else "(not readable or absent)")
        return EXIT_OK
    if command == "cleanup":
        if not args.yes:
            if as_json or not _confirm("Delete your Android data (apps' files) and leftover Android launcher entries?"):
                raise Refused("Confirmation required: run \"boswas-android cleanup --yes\".",
                              code="CONFIRMATION_REQUIRED")
        doc = runtime.cleanup_user(ctx)
        if as_json:
            _print_json(doc)
        else:
            c = doc["cleanup"]
            print(f"Removed {c['desktop_entries_removed']} launcher entries; Android data "
                  f"{'removed' if c['user_data_removed'] else 'not present or not fully removed'}.")
            for path in c["not_removed"]:
                print(f"  not removed: {path}")
        return EXIT_OK
    raise AndroidError(f"unknown command {command}")


def main(argv=None, ctx: Context | None = None) -> int:
    args = _parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        return run(args, ctx or Context(), as_json)
    except AndroidError as exc:
        if as_json:
            _print_json({"schema": JSON_SCHEMA, "error": {"code": exc.code, "message": exc.message,
                                                          "problems": exc.problems}})
        else:
            print(f"boswas-android: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:                 # never a traceback for the user
        message = f"internal error ({type(exc).__name__}: {str(exc)[:300]})"
        if as_json:
            _print_json({"schema": JSON_SCHEMA, "error": {"code": "INTERNAL", "message": message, "problems": []}})
        else:
            print(f"boswas-android: {message}", file=sys.stderr)
        return EXIT_INTERNAL
