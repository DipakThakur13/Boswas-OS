"""`boswas-device`: device identity, agent status, configuration and enrollment.

Exit codes (stable, docs/administration/cli.md):
  0   success
  1   failed, or `config validate` found problems
  2   usage error
  4   refused (root required, not permitted)
  5   the device agent service is not running (only for operations that need it)
  70  internal error

Read-only commands work without the service: they fall back to the agent's
public files in /var/lib/boswas/agent and say so.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import AgentConfig
from .errors import IdentityError
from .identity import IdentityStore
from .localapi import ApiClient, ApiError
from .paths import AgentPaths
from .storage import read_json

JSON_SCHEMA = "boswas-device/1"
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED, EXIT_UNAVAILABLE, EXIT_SOFTWARE = 0, 1, 2, 4, 5, 70


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_FAILED, reason: str = "failed"):
        super().__init__(message)
        self.code, self.reason = code, reason


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value) -> str:
    return "".join(c for c in str(value) if c.isprintable() or c == "\n")


def _rows(pairs) -> str:
    width = max(len(k) for k, _ in pairs) + 2
    return "\n".join(f"{(k + ':').ljust(width)}{'-' if v in (None, '') else _text(v)}" for k, v in pairs)


class Cli:
    def __init__(self, args, paths: AgentPaths):
        self.args, self.paths = args, paths
        self.client = ApiClient(paths.socket, timeout=args.timeout)

    def emit(self, command: str, payload: dict, text: str | None) -> None:
        if self.args.json:
            json.dump({"schema": JSON_SCHEMA, "command": command, "generated_at": _now(), **payload}, sys.stdout,
                      indent=2)
            sys.stdout.write("\n")
        elif text:
            sys.stdout.write(text.rstrip("\n") + "\n")

    def call(self, op: str, **params) -> dict:
        try:
            return self.client.call(op, **params)
        except ConnectionError as exc:
            raise CliError(f"the device agent is not running ({exc})", EXIT_UNAVAILABLE, "unavailable") from exc
        except ApiError as exc:
            code = EXIT_REFUSED if exc.code in ("FORBIDDEN", "REFUSED") else EXIT_FAILED
            raise CliError(exc.message, code, exc.code.lower()) from exc

    def try_call(self, op: str, **params) -> dict | None:
        try:
            return self.client.call(op, **params)
        except ConnectionError:
            return None
        except ApiError as exc:
            raise CliError(exc.message, EXIT_FAILED, exc.code.lower()) from exc

    # --- commands --------------------------------------------------------------------
    def status(self) -> int:
        status = self.try_call("agent.status")
        running = status is not None
        if status is None:
            status = read_json(self.paths.status) or {}
            if not status:
                raise CliError("the device agent has never run on this device (no status yet)", EXIT_UNAVAILABLE,
                               "unavailable")
        policy = status.get("policy") or {}
        self.emit("status", {"agent_running": running, "agent": status}, _rows([
            ("Device ID", status.get("device_id")),
            ("Device state", f"{status.get('state')}" + (f" ({'; '.join(status.get('reasons') or [])})"
                                                          if status.get("reasons") else "")),
            ("Agent", f"{'running' if running else 'NOT RUNNING (last known status)'}, version "
                      f"{status.get('agent_version')}"),
            ("Connection", status.get("connection")),
            ("Control plane", status.get("control_plane") or "none (standalone device)"),
            ("Enrollment", status.get("enrollment")),
            ("Last contact", status.get("last_contact") or "never"),
            ("Last error", status.get("last_error")),
            ("Policy", policy.get("version") or "local (not managed)"),
            ("Remote commands", "paused (maintenance)" if status.get("maintenance") else
             ("enabled" if status.get("remote_commands") else "disabled")),
            ("Pending messages", sum((status.get("outbox") or {}).values())),
            ("Updated", status.get("updated_at")),
        ]))
        return EXIT_OK

    def identity(self) -> int:
        if getattr(self.args, "action", None) == "reset":
            if not self.args.yes:
                raise CliError("this creates a NEW device identity; repeat with --yes to confirm", EXIT_USAGE,
                               "usage")
            result = self.call("identity.reset", confirm=True)
            self.emit("identity-reset", {"identity": result}, f"new device ID: {result.get('device_id')}")
            return EXIT_OK
        identity = self.try_call("device.identity")
        if identity is None:
            try:
                ident = IdentityStore(self.paths).load()
            except IdentityError as exc:
                raise CliError(str(exc), EXIT_FAILED, "identity-invalid") from exc
            if ident is None:
                raise CliError("no device identity yet (the agent creates it at its first start)", EXIT_UNAVAILABLE,
                               "unavailable")
            identity = ident.to_dict()
        self.emit("identity", {"identity": identity}, _rows([
            ("Device ID", identity.get("device_id")),
            ("Created", identity.get("created_at")),
            ("Source", identity.get("source")),
            ("Lifetime", "ephemeral (live session)" if identity.get("ephemeral") else "persistent"),
            ("Enrollment", identity.get("enrollment")),
            ("Certificate", identity.get("certificate_fingerprint") or "none"),
        ]))
        return EXIT_OK

    def config(self) -> int:
        if self.args.action == "validate":
            path = Path(self.args.file) if self.args.file else self.paths.device_conf
            cfg = AgentConfig.load(path, check_owner=0 if (os.geteuid() == 0 and not self.args.file) else None)
            lines = [f"{cfg.path}: {'valid' if cfg.valid else 'INVALID'}"]
            lines += [f"  error:   {p}" for p in cfg.problems]
            lines += [f"  warning: {w}" for w in cfg.warnings]
            self.emit("config-validate", {"config": cfg.to_dict()}, "\n".join(lines))
            return EXIT_OK if cfg.valid else EXIT_FAILED
        result = self.try_call("device.config") or AgentConfig.load(self.paths.device_conf).to_dict()
        text = "\n".join(f'{k}="{v}"' for k, v in result["settings"].items())
        if result.get("problems"):
            text += "\n\n" + "\n".join(f"error: {p}" for p in result["problems"])
        self.emit("config-show", {"config": result}, text)
        return EXIT_OK

    def inventory(self) -> int:
        if self.args.refresh:
            self.call("inventory.refresh")
        doc = self.try_call("device.inventory") or read_json(self.paths.inventory)
        if not isinstance(doc, dict) or not doc.get("os"):
            raise CliError("no inventory yet", EXIT_UNAVAILABLE, "unavailable")
        osd, hw = doc.get("os", {}), doc.get("hardware") or {}
        apps = doc.get("windows_applications") or []
        lines = [_rows([
            ("Revision", doc.get("revision")),
            ("Collected", doc.get("collected_at")),
            ("OS", f"{osd.get('name')} {osd.get('version')} ({osd.get('version_id')}), package base "
                   f"{osd.get('debian_version')}"),
            ("Kernel", osd.get("kernel")),
            ("Architecture", osd.get("architecture")),
            ("CPU", f"{hw.get('cpu_model')} ({hw.get('cpu_count')} threads)" if hw else None),
            ("Memory", f"{hw.get('memory_gib')} GiB" if hw.get("memory_gib") else None),
            ("Windows runtime", f"Wine {(doc.get('compatibility') or {}).get('wine_version')}, "
                                f"{', '.join((doc.get('compatibility') or {}).get('architectures') or [])} only"),
        ])]
        if apps:
            lines.append("\nWindows applications:")
            lines += [f"  {a['id']} {a.get('version') or ''} {a.get('app_state')} "
                      f"({a.get('installations')} installation(s))" for a in apps]
        self.emit("inventory", {"inventory": doc}, "\n".join(lines))
        return EXIT_OK

    def policy(self) -> int:
        result = self.try_call("policy.status")
        if result is None:
            status = read_json(self.paths.status) or {}
            result = {"policy": status.get("policy") or {"managed": False}, "compat": None}
        pol = result["policy"]
        compat = result.get("compat") or {}
        self.emit("policy", result, _rows([
            ("Managed", "yes" if pol.get("managed") else "no (local policy)"),
            ("Version", pol.get("version")),
            ("Applied", pol.get("applied_at")),
            ("WinCompat policy", compat.get("source")),
            ("Allowed statuses", " ".join(compat.get("allowed_statuses") or []) if compat else None),
            ("Unlisted apps", compat.get("unlisted_apps") if compat else None),
            ("AppArmor required", ("yes" if compat.get("require_apparmor") else "no") if compat else None),
        ]))
        return EXIT_OK

    def commands(self) -> int:
        result = self.call("commands.list", limit=self.args.limit)
        lines = [f"{c.get('received_at') or '-':20}  {c.get('type') or '-':20}  {c.get('status') or '-':12}  "
                 f"{c.get('application_id') or ''}" for c in result["commands"]]
        self.emit("commands", result, "\n".join(lines) or "No remote commands received.")
        return EXIT_OK

    def events(self) -> int:
        result = self.call("events.list", limit=self.args.limit)
        lines = [f"{e.get('occurred_at')}  {e.get('type'):22}  {e.get('detail')}" for e in result["events"]]
        self.emit("events", result, "\n".join(lines) or "No events.")
        return EXIT_OK

    def enroll(self) -> int:
        if self.args.token_file:
            path = Path(self.args.token_file)
            try:
                st = path.stat()
                token = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise CliError(f"cannot read the token file: {exc.strerror}", EXIT_FAILED, "not-found") from exc
            if st.st_mode & 0o077:
                sys.stderr.write("boswas-device: warning: the token file is readable by other users; delete it now\n")
        else:
            token = sys.stdin.readline().strip()
        result = self.call("enroll", token=token)
        self.emit("enroll", result, _rows([("Enrolled", "yes"), ("Device ID", result.get("device_id")),
                                           ("Control plane", result.get("control_plane")),
                                           ("Certificate", result.get("certificate_fingerprint"))]))
        return EXIT_OK

    def unenroll(self) -> int:
        if not self.args.yes:
            raise CliError("this disconnects the device from its Control Plane; repeat with --yes", EXIT_USAGE, "usage")
        self.emit("unenroll", self.call("unenroll"), "unenrolled; the local WinCompat policy applies again")
        return EXIT_OK

    def sync(self) -> int:
        self.emit("sync", self.call("sync.now"), "synchronisation scheduled")
        return EXIT_OK

    def maintenance(self) -> int:
        result = self.call("maintenance.set", enabled=self.args.mode == "on")
        self.emit("maintenance", result, f"maintenance mode {self.args.mode}")
        return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    # No abbreviated options: "--token" must never be taken for "--token-file"
    # (a token typed on the command line would end up in an error message).
    common = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    parser = argparse.ArgumentParser(prog="boswas-device", parents=[common], allow_abbrev=False,
                                     description="Boswas device identity, agent status, configuration and enrollment.")
    parser.add_argument("--version", action="version", version=f"boswas-device {__version__}")
    parser.add_argument("--timeout", type=float, default=60, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name, help_text):
        return sub.add_parser(name, parents=[common], help=help_text, description=help_text, allow_abbrev=False)

    add("status", "device state, agent and Control Plane connection")
    p = add("identity", "the persistent device identity")
    isub = p.add_subparsers(dest="action", metavar="[reset]")
    r = isub.add_parser("reset", parents=[common], help="create a new device identity (root; unenrolled only)")
    r.add_argument("--yes", action="store_true")
    p = add("config", "device configuration (/etc/boswas/device.conf)")
    csub = p.add_subparsers(dest="action", metavar="<action>", required=True)
    v = csub.add_parser("validate", parents=[common], help="check device.conf")
    v.add_argument("--file", help="check another file instead")
    csub.add_parser("show", parents=[common], help="effective settings")
    p = add("inventory", "the device inventory (what is reported, and nothing else)")
    p.add_argument("--refresh", action="store_true", help="collect now (root)")
    add("policy", "applied device policy")
    p = add("commands", "remote commands received and their outcome")
    p.add_argument("--limit", type=int, default=50)
    p = add("events", "device event log")
    p.add_argument("--limit", type=int, default=100)
    p = add("enroll", "enroll with the Control Plane in device.conf (root); the one-time token is read from "
                      "--token-file or standard input, never from the command line")
    p.add_argument("--token-file")
    p = add("unenroll", "leave the Control Plane (root)")
    p.add_argument("--yes", action="store_true")
    add("sync", "contact the Control Plane now (root)")
    p = add("maintenance", "pause or resume remote commands (root)")
    p.add_argument("mode", choices=("on", "off"))
    return parser


def main(argv: list[str] | None = None, paths: AgentPaths | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    if not args.command:
        parser.print_help()
        return EXIT_USAGE
    cli = Cli(args, paths or AgentPaths())
    try:
        return getattr(cli, args.command)()
    except CliError as exc:
        if args.json:
            cli.emit(args.command, {"error": {"reason": exc.reason, "message": str(exc)}}, None)
        else:
            sys.stderr.write(f"boswas-device: {_text(exc)}\n")
        return exc.code
    except BrokenPipeError:
        return EXIT_OK
    except Exception as exc:
        sys.stderr.write(f"boswas-device: internal error: {type(exc).__name__}: {_text(exc)}\n")
        return EXIT_SOFTWARE
