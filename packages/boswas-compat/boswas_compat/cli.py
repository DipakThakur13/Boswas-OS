"""`boswas-winapp` command-line interface.

Exit codes (stable, docs/administration/cli.md):
  0   success
  1   the operation failed (e.g. the installer failed)
  2   usage error
  3   application, installer or manifest not found
  4   refused by policy or a safety rule (root, blocked, unlisted, ...)
  5   runtime unavailable (Wine, bubblewrap, or AppArmor confinement)
  6   busy (the application is running or locked)
  70  internal error
For `launch`, once the program has started, the exit code is the Windows
program's own (values above 255 are reported as 1); a launch ended by
`boswas-winapp stop` exits 0 and reports "stopped": true.
For `inspect`, 0 means the installer would be accepted and 4 that it would
be refused; both print the full decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import JSON_SCHEMA, __version__, ops
from . import manifest as mf
from .errors import (EXIT_FAILED, EXIT_OK, EXIT_REFUSED, EXIT_SOFTWARE, EXIT_USAGE, ManifestError, WinAppError)
from .executor import sanitize


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _emit(args, command: str, payload: dict, text: str | None) -> None:
    if args.json:
        doc = {"schema": JSON_SCHEMA, "command": command, "generated_at": _now(), **payload}
        json.dump(doc, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif text:
        sys.stdout.write(text.rstrip("\n") + "\n")


def _text(value: object) -> str:
    """Values printed as text may come from records or file names the
    application chose: never let them carry terminal control sequences."""
    return sanitize(str(value)).replace("\n", " ")


def _table(rows: list[list[str]], header: list[str]) -> str:
    rows = [[_text(c) for c in r] for r in rows]
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(str(c).ljust(w) for c, w in zip(header, widths)).rstrip()]
    lines += ["  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    return "\n".join(lines)


def _rows(pairs: list[tuple[str, object]]) -> str:
    width = max(len(k) for k, _ in pairs) + 2
    return "\n".join(f"{(k + ':').ljust(width)}{'-' if v in (None, '') else _text(v)}" for k, v in pairs)


def _sandbox_text(sb: dict) -> str:
    granted = [k for k in ("network", "display", "audio", "gpu") if sb.get(k)]
    granted += [f"folder:{f}" for f in sb.get("folders", [])]
    return ", ".join(granted) if granted else "own prefix only"


# --- commands -----------------------------------------------------------------------

def cmd_install(args, ctx) -> int:
    result = ops.install(ctx, args.installer, app_id=args.id, name=args.name, portable=args.portable,
                         interactive=args.interactive, installer_args=args.args or None,
                         timeout=args.timeout, verbose=args.verbose)
    app = result["application"]
    _emit(args, "install", result, _rows([
        ("Application", f"{app['name']} ({app['id']})"),
        ("Version", app["version"]),
        ("Status", f"{app['status']} ({app['manifest_source']})"),
        ("Program", app.get("launch") or "ambiguous: " + ", ".join(app.get("launch_candidates") or [])),
        ("Sandbox", _sandbox_text(result["sandbox"])),
        ("Log", result["log"]),
    ]))
    return EXIT_OK


def cmd_inspect(args, ctx) -> int:
    result = ops.inspect_installer(ctx, args.installer, app_id=args.id, name=args.name, portable=args.portable)
    inst, arch, decision, app = result["installer"], result["architecture"], result["decision"], result["application"]
    rows = [
        ("Installer", inst["file"]),
        ("Type", inst["kind"] + (f" ({inst['framework']})" if inst.get("framework") else "")),
        ("Architecture", (arch["detected"] or "not determined") +
         ("" if arch["supported"] is None else (" (supported)" if arch["supported"] else " (NOT supported)"))),
        ("SHA-256", inst["sha256"]),
    ]
    if app:
        rows += [("Application", f"{app['name']} ({app['id']})"),
                 ("Status", f"{app['status']} ({app['manifest_source']})"),
                 ("Sandbox", _sandbox_text(result["sandbox"]))]
    rows.append(("Decision", "would be installed" if decision["allowed"] else f"refused: {decision['message']}"))
    _emit(args, "inspect", result, _rows(rows))
    return EXIT_OK if decision["allowed"] else EXIT_REFUSED


def cmd_upgrade(args, ctx) -> int:
    result = ops.upgrade(ctx, args.application, args.installer, interactive=args.interactive,
                         installer_args=args.args or None, timeout=args.timeout, verbose=args.verbose)
    app = result["application"]
    _emit(args, "upgrade", result, _rows([
        ("Application", f"{app['name']} ({app['id']})"),
        ("Version", f"{result['previous']['version']} -> {app['version']}"),
        ("Status", f"{app['status']} ({app['manifest_source']})"),
        ("Log", result["log"]),
    ]))
    return EXIT_OK


def cmd_stop(args, ctx) -> int:
    result = ops.stop(ctx, args.application)
    text = f"stopped {args.application}" if result["stopped"] else f"{args.application} is not running"
    _emit(args, "stop", result, text)
    return EXIT_OK


def cmd_runtime(args, ctx) -> int:
    r = ops.runtime_info(ctx)
    aa = r["apparmor"]
    _emit(args, "runtime", r, _rows([
        ("Wine", (r["wine"]["version"] or "unknown version") if r["wine"]["available"] else "MISSING"),
        ("Architectures", ", ".join(r["architectures"]) + " (64-bit Windows applications only)"),
        ("Bubblewrap", (r["bubblewrap"]["version"] or "available") if r["bubblewrap"]["available"] else "MISSING"),
        ("AppArmor", f"{aa['profile']}: {aa['mode']}" + ("" if aa["required"] else " (not required by policy)")),
        ("Policy", f"{r['policy']['source']}" + (" (managed)" if r["policy"]["managed"] else "")),
        ("Healthy", "yes" if r["healthy"] else "no"),
    ]))
    return EXIT_OK if r["healthy"] else EXIT_FAILED


def cmd_remove(args, ctx) -> int:
    _emit(args, "remove", ops.remove(ctx, args.application), None)
    return EXIT_OK


def cmd_list(args, ctx) -> int:
    result = ops.list_apps(ctx, all_users=args.all_users)
    if args.all_users:
        rows = [[u["user"], a.get("id"), a.get("version") or "-", a.get("status") or "-", a.get("state") or "-"]
                for u in result["users"] for a in u["applications"]]
        text = _table(rows, ["USER", "APPLICATION", "VERSION", "STATUS", "STATE"]) if rows else \
            "No Windows applications installed for any user."
    else:
        rows = [[a["id"], a.get("name") or "-", a.get("version") or "-", a.get("status") or "-",
                 a.get("state") or "-", "running" if a.get("running") else ""] for a in result["applications"]]
        text = _table(rows, ["APPLICATION", "NAME", "VERSION", "STATUS", "STATE", ""]) if rows else \
            "No Windows applications installed."
    _emit(args, "list", result, text)
    return EXIT_OK


def cmd_launch(args, ctx) -> int:
    mirror = None if (args.json or args.quiet) else sys.stdout
    result = ops.launch(ctx, args.application, args.args, exe=args.exe, timeout=args.timeout,
                        mirror=mirror)
    _emit(args, "launch", result, None)
    code = result["exit_code"]
    if result["timed_out"]:
        sys.stderr.write(f"boswas-winapp: {args.application} stopped after the {args.timeout:.0f} s timeout\n")
        return EXIT_FAILED
    if result.get("stopped"):
        if not args.json:
            sys.stderr.write(f"boswas-winapp: {args.application} was stopped on request\n")
        return EXIT_OK
    return code if isinstance(code, int) and 0 <= code <= 255 else EXIT_FAILED


def cmd_status(args, ctx) -> int:
    result = ops.status(ctx, args.application)
    app, last = result["application"], result.get("last_launch") or {}
    policy = result["policy"]
    _emit(args, "status", result, _rows([
        ("Application", f"{app.get('name')} ({app['id']})"),
        ("Version", app.get("version")),
        ("Status", f"{app['status']} ({app.get('manifest_source')})"),
        ("State", app.get("state") + (" (running)" if result["running"] else "")),
        ("Allowed", "yes" if policy["allowed"] else f"no: {policy['reason']}"),
        ("Program", app.get("launch") or "none (use --exe)"),
        ("Sandbox", _sandbox_text(result["sandbox"])),
        ("Wine", (result.get("runtime") or {}).get("wine")),
        ("Installed", app.get("installed_at")),
        ("Last launch", f"{last.get('at')} exit {last.get('exit_code')}" if last else None),
        ("Confinement", last.get("confinement")),
        ("Prefix", result["paths"]["prefix"]),
        ("Sandbox view", result["paths"]["sandbox_view"]),
    ]))
    return EXIT_OK


def cmd_repair(args, ctx) -> int:
    result = ops.repair(ctx, args.application, timeout=args.timeout)
    _emit(args, "repair", result, None)
    return EXIT_OK if result["healthy"] else EXIT_FAILED


def cmd_logs(args, ctx) -> int:
    if args.clear:
        result = ops.clear_logs(ctx, args.application)
        _emit(args, "logs-clear", result, f"removed {result['removed']} log file(s)")
        return EXIT_OK
    kind = "install" if args.install else ("repair" if args.repair else ("launch" if args.launch else None))
    if kind is None:
        try:
            result = ops.logs(ctx, args.application, kind="launch", lines=args.lines)
        except WinAppError:
            result = ops.logs(ctx, args.application, lines=args.lines)
    else:
        result = ops.logs(ctx, args.application, kind=kind, lines=args.lines)
    _emit(args, "logs", result, f"==> {result['log']} <==\n" + "\n".join(result["lines"]))
    return EXIT_OK


def cmd_catalog(args, ctx) -> int:
    result = ops.catalog_list(ctx)
    rows = [[m["id"], m["version"], m["status"], m["layer"], _sandbox_text(m["sandbox"])] for m in result["manifests"]]
    text = _table(rows, ["APPLICATION", "VERSION", "STATUS", "CATALOG", "SANDBOX"]) if rows else \
        "The compatibility catalog is empty."
    if result["problems"]:
        text += "\n\nIgnored manifests:\n" + "\n".join(f"  {p}" for p in result["problems"])
    _emit(args, "catalog", result, text)
    return EXIT_OK


def cmd_validate(args, ctx) -> int:
    from pathlib import Path
    results, failed = [], False
    for file in args.files:
        try:
            m = mf.load(Path(file))
            results.append({"file": file, "valid": True, "id": m.id, "status": m.status, "problems": []})
        except ManifestError as exc:
            failed = True
            results.append({"file": file, "valid": False, "problems": exc.problems or [str(exc)]})
    text = "\n".join(f"{r['file']}: " + ("valid" if r["valid"] else "INVALID\n    " + "\n    ".join(r["problems"]))
                     for r in results)
    _emit(args, "manifest-validate", {"results": results}, text)
    return EXIT_FAILED if failed else EXIT_OK


# --- parsing ---------------------------------------------------------------------------

NO_CONTEXT = {cmd_validate}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable JSON output")

    parser = argparse.ArgumentParser(
        prog="boswas-winapp", parents=[common],
        description="Install and run Windows applications in isolated, confined Wine sandboxes.")
    parser.add_argument("--version", action="version", version=f"boswas-winapp {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name, handler, help_text):
        p = sub.add_parser(name, parents=[common], help=help_text, description=help_text)
        p.set_defaults(handler=handler)
        return p

    p = add("install", cmd_install, "install a Windows application from an installer (.exe, .msi); "
                                    "installer arguments follow --")
    p.add_argument("installer", help="installer file")
    p.add_argument("--id", help="application ID (catalog ID, or a local.* name for unlisted applications)")
    p.add_argument("--name", help="display name for an unlisted application")
    p.add_argument("--portable", action="store_true", help="the file is the application itself: copy, do not run it")
    p.add_argument("--interactive", action="store_true", help="show the installer's own dialogs (no silent switches)")
    p.add_argument("--timeout", type=float, help="stop the installer after this many seconds")
    p.add_argument("--verbose", action="store_true", help="also print the installer's output")
    p.add_argument("--pause", action="store_true", help="wait for Enter before exiting (desktop launcher)")

    p = add("inspect", cmd_inspect, "check an installer without installing it: type, architecture, "
                                    "catalog match and policy decision (nothing is run)")
    p.add_argument("installer", help="installer file")
    p.add_argument("--id", help="application ID it would be installed as")
    p.add_argument("--name", help="display name for an unlisted application")
    p.add_argument("--portable", action="store_true", help="the file is the application itself")

    p = add("upgrade", cmd_upgrade, "run a newer installer of an installed application in its prefix; "
                                    "installer arguments follow --")
    p.add_argument("application")
    p.add_argument("installer", help="installer file of the newer version")
    p.add_argument("--interactive", action="store_true", help="show the installer's own dialogs (no silent switches)")
    p.add_argument("--timeout", type=float, help="stop the installer after this many seconds")
    p.add_argument("--verbose", action="store_true", help="also print the installer's output")

    p = add("remove", cmd_remove, "remove an application and its prefix")
    p.add_argument("application")

    p = add("stop", cmd_stop, "stop a running application (ends all of its Windows processes)")
    p.add_argument("application")

    add("runtime", cmd_runtime, "show the Windows compatibility runtime: Wine, bubblewrap, AppArmor, policy")

    p = add("list", cmd_list, "list installed Windows applications")
    p.add_argument("--all-users", action="store_true", help="every user's applications (root; inventory)")

    p = add("launch", cmd_launch, "start an installed application in its sandbox; program arguments follow --")
    p.add_argument("application")
    p.add_argument("--exe", help="program to start instead of the default (path below C:\\)")
    p.add_argument("--timeout", type=float, help="stop the application after this many seconds")
    p.add_argument("--quiet", action="store_true", help="do not print the program's output (it is still logged)")

    p = add("status", cmd_status, "show an application's state, sandbox and policy decision")
    p.add_argument("application")

    p = add("repair", cmd_repair, "check and repair an application's prefix and launcher")
    p.add_argument("application")
    p.add_argument("--timeout", type=float, help="stop each repair step after this many seconds")

    p = add("logs", cmd_logs, "show an application's latest log")
    p.add_argument("application")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--install", action="store_true", help="the latest installation log")
    group.add_argument("--repair", action="store_true", help="the latest repair log")
    group.add_argument("--launch", action="store_true", help="the latest launch log only (default: launch, else any)")
    group.add_argument("--clear", action="store_true", help="delete the application's logs")
    p.add_argument("--lines", type=int, default=200, help="number of lines (0: all; default 200)")

    add("catalog", cmd_catalog, "list the compatibility catalog and the effective policy")

    mp = sub.add_parser("manifest", parents=[common], help="compatibility manifest tools",
                        description="compatibility manifest tools")
    msub = mp.add_subparsers(dest="action", metavar="<action>", required=True)
    v = msub.add_parser("validate", parents=[common], help="validate manifest files", description="validate manifest files")
    v.add_argument("files", nargs="+")
    v.set_defaults(handler=cmd_validate)
    return parser


PASSTHROUGH = {"install", "launch", "upgrade"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after the first "--" goes to the installer or program as is.
    passthrough: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, passthrough = argv[:cut], argv[cut + 1:]
    args = parser.parse_args(argv)
    if passthrough and getattr(args, "command", None) not in PASSTHROUGH:
        parser.error("arguments after -- are only accepted by install, upgrade and launch")
    args.args = passthrough
    if not hasattr(args, "json"):
        args.json = False
    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_USAGE
    code = EXIT_SOFTWARE
    try:
        ctx = None if args.handler in NO_CONTEXT else ops.Context.create(out=None if args.json else sys.stderr)
        code = args.handler(args, ctx)
    except WinAppError as exc:
        if args.json:
            _emit(args, args.command, {"error": {"reason": exc.reason, "message": str(exc),
                                                 **({"problems": exc.problems} if isinstance(exc, ManifestError) else {})}}, None)
        else:
            sys.stderr.write(f"boswas-winapp: {_text(exc)}\n")
        code = exc.exit_code
    except BrokenPipeError:
        code = EXIT_OK
    except KeyboardInterrupt:
        sys.stderr.write("boswas-winapp: interrupted\n")
        code = 130
    except Exception as exc:  # report, never dump a traceback with local state
        sys.stderr.write(f"boswas-winapp: internal error: {type(exc).__name__}: {_text(exc)}\n")
        code = EXIT_SOFTWARE
    if getattr(args, "pause", False) and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
