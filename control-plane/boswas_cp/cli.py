"""`boswas-cp`: set up, run and administer the Boswas Control Plane.

  boswas-cp init --public-url URL --server-name NAME... [--admin NAME]
  boswas-cp serve
  boswas-cp operator add NAME --role viewer|operator|admin | list | disable NAME
  boswas-cp token create [--profile P] [--policy NAME] [--ttl-hours N] [--max-uses N] [--allow-ephemeral]
                   [--device DEVICE_ID]
  boswas-cp artifact add FILE [--name FILE_NAME]
  boswas-cp catalog add MANIFEST.json [--installer FILE] | list
  boswas-cp policy publish POLICY.json | list
  boswas-cp device list | retire DEVICE_ID
  boswas-cp events list [--limit N] | verify
  boswas-cp ca-certificate          the CA devices pin (CONTROL_PLANE_CA)
  boswas-cp sweep

Administration commands work directly on the data directory (run them as
the service user or root on the Control Plane host). Secrets (operator and
enrollment tokens) are printed once and stored only as hashes.

Configuration: /etc/boswas-control-plane/control-plane.conf (KEY="value").
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .api import DEVICE, OPERATOR, Api
from .auth import LocalTokenAuthenticator, Principal
from .pki import Pki
from .server import serve
from .service import ControlPlane, ServiceError
from .store import Store
from .sweeper import Sweeper

CONFIG = Path("/etc/boswas-control-plane/control-plane.conf")
DEFAULTS = {"LISTEN_ADDRESS": "0.0.0.0", "LISTEN_PORT": "8443", "PUBLIC_URL": "",
            "OPERATOR_LISTEN_ADDRESS": "0.0.0.0", "OPERATOR_LISTEN_PORT": "9443",
            "DATA_DIR": "/var/lib/boswas-control-plane", "DASHBOARD_DIR": "/usr/share/boswas-control-plane/dashboard",
            "DEVICE_CERT_DAYS": "365", "OFFLINE_AFTER_SECONDS": "180", "MAX_UPLOAD_MB": "4096", "LOG_LEVEL": "info"}
LOCAL_ADMIN = Principal("local-admin:boswas-cp", "admin")


@dataclass
class Settings:
    values: dict

    @classmethod
    def load(cls, path: Path) -> "Settings":
        values = dict(DEFAULTS)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() in DEFAULTS:
                values[key.strip()] = " ".join(shlex.split(value, comments=True))
        for env, key in (("BOSWAS_CP_DATA_DIR", "DATA_DIR"), ("BOSWAS_CP_PUBLIC_URL", "PUBLIC_URL")):
            if os.environ.get(env):
                values[key] = os.environ[env]
        return cls(values)

    def __getitem__(self, key: str) -> str:
        return self.values[key]

    def number(self, key: str) -> int:
        value = self.values[key]
        if not value.isdigit():
            raise SystemExit(f"boswas-cp: {key} must be a number")
        return int(value)


def open_control_plane(settings: Settings) -> ControlPlane:
    data = Path(settings["DATA_DIR"])
    if not (data / "control-plane.db").exists():
        raise SystemExit(f"boswas-cp: no Control Plane in {data}; run 'boswas-cp init' first")
    return ControlPlane(Store(data / "control-plane.db"), Pki(data), public_url=settings["PUBLIC_URL"] or "https://unset",
                        artifacts_dir=data / "artifacts", device_cert_days=settings.number("DEVICE_CERT_DAYS"),
                        offline_floor=settings.number("OFFLINE_AFTER_SECONDS"),
                        max_artifact_bytes=settings.number("MAX_UPLOAD_MB") * 1024 * 1024)


def emit(args, payload, text: str) -> None:
    if args.json:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(text.rstrip("\n") + "\n")


# --- commands --------------------------------------------------------------------------------

def cmd_init(args, settings: Settings) -> int:
    url = args.public_url or settings["PUBLIC_URL"]
    if not re.fullmatch(r"https://[A-Za-z0-9.:\[\]-]+(:\d+)?(/[A-Za-z0-9._/-]*)?", url or ""):
        raise SystemExit("boswas-cp: --public-url must be the https:// URL devices use")
    data = Path(settings["DATA_DIR"])
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(data, 0o700)
    (data / "artifacts").mkdir(mode=0o750, exist_ok=True)
    pki = Pki(data)
    pki.initialize(args.server_name)
    store = Store(data / "control-plane.db")
    os.chmod(data / "control-plane.db", 0o600)
    cp = ControlPlane(store, pki, public_url=url, artifacts_dir=data / "artifacts")
    cp.ensure_default_policy()
    result = {"data_dir": str(data), "public_url": url, "server_ca": str(pki.server_ca_crt)}
    if not store.one("SELECT 1 FROM operators LIMIT 1"):
        result["admin"] = cp.add_operator(LOCAL_ADMIN.actor, args.admin, "admin")
    text = (f"Control Plane initialised in {data}\n"
            f"Devices pin this CA (CONTROL_PLANE_CA): {pki.server_ca_crt}\n")
    if "admin" in result:
        text += (f"Administrator '{args.admin}' API token (shown once; store it safely):\n"
                 f"  {result['admin']['token']}\n")
    emit(args, result, text)
    return 0


def cmd_serve(args, settings: Settings) -> int:
    cp = open_control_plane(settings)
    if not settings["PUBLIC_URL"]:
        raise SystemExit("boswas-cp: set PUBLIC_URL in the configuration")
    cp.public_url = settings["PUBLIC_URL"].rstrip("/")
    api = Api(cp, LocalTokenAuthenticator(cp.store), dashboard=Path(settings["DASHBOARD_DIR"]),
              max_upload=settings.number("MAX_UPLOAD_MB") * 1024 * 1024)
    listeners = [(DEVICE, settings["LISTEN_ADDRESS"], settings.number("LISTEN_PORT"), cp.pki.server_context())]
    if settings["OPERATOR_LISTEN_PORT"]:                  # empty: no operator API (administer with boswas-cp)
        listeners.append((OPERATOR, settings["OPERATOR_LISTEN_ADDRESS"], settings.number("OPERATOR_LISTEN_PORT"),
                          cp.pki.operator_context()))
        if listeners[1][2] == listeners[0][2]:
            raise SystemExit("boswas-cp: LISTEN_PORT and OPERATOR_LISTEN_PORT must differ")
    servers = [serve(api, context, host, port, listener) for listener, host, port, context in listeners]
    sweeper = Sweeper(cp)
    sweeper.start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log = logging.getLogger("boswas-control-plane")
    for listener, host, port, _ in listeners:
        log.info("%s port listening on %s:%s", listener, host, port)
        sys.stdout.write(f"boswas-cp: {listener} port on https://{host}:{port}\n")
    log.info("devices use %s", cp.public_url)
    sys.stdout.flush()
    stop.wait()
    sweeper.stop()
    for server in servers:
        server.shutdown()
        server.server_close()
    return 0


def cmd_operator(args, settings: Settings) -> int:
    cp = open_control_plane(settings)
    if args.action == "add":
        result = cp.add_operator(LOCAL_ADMIN.actor, args.name, args.role)
        emit(args, result, f"operator {args.name} ({args.role}) API token (shown once):\n  {result['token']}")
    elif args.action == "disable":
        emit(args, cp.disable_operator(LOCAL_ADMIN.actor, args.name), f"operator {args.name} disabled")
    else:
        ops = cp.list_operators()
        emit(args, {"operators": ops}, "\n".join(f"{o['name']:20} {o['role']:9} {'disabled' if o['disabled'] else ''}"
                                                 for o in ops) or "no operators")
    return 0


def cmd_token(args, settings: Settings) -> int:
    cp = open_control_plane(settings)
    result = cp.create_enrollment_token(LOCAL_ADMIN, profile=args.profile, policy_name=args.policy,
                                        ttl_hours=args.ttl_hours, allow_ephemeral=args.allow_ephemeral,
                                        max_uses=args.max_uses, description=args.description,
                                        device_id=args.device)
    emit(args, result, f"enrollment token (shown once; expires {result['expires_at']}, {result['max_uses']} use(s)):\n"
                       f"  {result['token']}")
    return 0


def cmd_artifact(args, settings: Settings) -> int:
    cp = open_control_plane(settings)
    row = cp.import_artifact_file(LOCAL_ADMIN, Path(args.file), args.name)
    emit(args, row, f"stored {row['file_name']} ({row['kind']}, {row['machine'] or 'architecture unknown'}) "
                    f"sha256 {row['sha256']}")
    return 0


def cmd_catalog(args, settings: Settings) -> int:
    cp = open_control_plane(settings)
    if args.action == "list":
        apps = cp.list_applications()
        emit(args, {"applications": apps}, "\n".join(f"{a['app_id']:36} {a['version']:12} {a['status']:12} "
                                                     f"{a['support']}" for a in apps) or "the catalog is empty")
        return 0
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if args.installer:
        cp.import_artifact_file(LOCAL_ADMIN, Path(args.installer))
    app = cp.upsert_application(LOCAL_ADMIN, {"manifest": manifest})
    emit(args, app, f"{app['app_id']} {app['version']} in the catalog ({app['support']})")
    return 0


def cmd_policy(args, settings: Settings) -> int:
    cp = open_control_plane(settings)
    if args.action == "list":
        pols = cp.list_policies()
        emit(args, {"policies": pols}, "\n".join(f"{p['version']:24} {p['devices']} device(s)" for p in pols))
        return 0
    body = json.loads(Path(args.file).read_text(encoding="utf-8"))
    policy = cp.publish_policy(LOCAL_ADMIN, body)
    emit(args, policy, f"published {policy['version']} (signed)")
    return 0


def cmd_device(args, settings: Settings) -> int:
    cp = open_control_plane(settings)
    if args.action == "retire":
        emit(args, cp.retire_device(LOCAL_ADMIN, args.device_id), f"device {args.device_id} retired")
        return 0
    devices = cp.list_devices()
    emit(args, {"devices": devices}, "\n".join(f"{d['device_id']}  {d['status']:8} {d['connection']:8} "
                                               f"{d['name'] or ''}" for d in devices) or "no devices")
    return 0


def cmd_events(args, settings: Settings) -> int:
    cp = open_control_plane(settings)
    if args.action == "verify":
        result = cp.verify_events()
        emit(args, result, f"audit trail {'intact' if result['valid'] else 'BROKEN at event ' + str(result['first_invalid_event'])}"
                           f" ({result['events']} events)")
        return 0 if result["valid"] else 1
    events = cp.events(limit=args.limit)
    emit(args, {"events": events}, "\n".join(f"{e['occurred_at']} {e['type']:24} {e['actor']:30} "
                                             f"{e['device_id'] or ''}" for e in events))
    return 0


def cmd_ca(args, settings: Settings) -> int:
    pki = Pki(Path(settings["DATA_DIR"]))
    sys.stdout.write(pki.server_ca_crt.read_text())
    return 0


def cmd_sweep(args, settings: Settings) -> int:
    emit(args, open_control_plane(settings).sweep(), "sweep done")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p = argparse.ArgumentParser(prog="boswas-cp", parents=[common], allow_abbrev=False,
                                description="Boswas Control Plane administration.")
    p.add_argument("--config", default=str(CONFIG))
    p.add_argument("--version", action="version", version=f"boswas-cp {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def add(name, handler, help_text):
        s = sub.add_parser(name, parents=[common], help=help_text, allow_abbrev=False)
        s.set_defaults(handler=handler)
        return s

    s = add("init", cmd_init, "create keys, certificates, the database and the first administrator")
    s.add_argument("--public-url", help="https:// URL devices use")
    s.add_argument("--server-name", action="append", required=True, help="DNS name or IP of the server (repeat)")
    s.add_argument("--admin", default="admin", help="name of the first administrator")
    add("serve", cmd_serve, "run the Control Plane")
    s = add("operator", cmd_operator, "operator accounts")
    s.add_argument("action", choices=("add", "list", "disable"))
    s.add_argument("name", nargs="?")
    s.add_argument("--role", choices=("viewer", "operator", "admin"), default="viewer")
    s = add("token", cmd_token, "enrollment tokens")
    s.add_argument("action", choices=("create",))
    s.add_argument("--profile")
    s.add_argument("--policy")
    s.add_argument("--ttl-hours", type=int, default=24)
    s.add_argument("--max-uses", type=int, default=1)
    s.add_argument("--allow-ephemeral", action="store_true")
    s.add_argument("--description")
    s.add_argument("--device", help="only this device may enroll with it (required to re-enroll an enrolled device)")
    s = add("artifact", cmd_artifact, "store an installer")
    s.add_argument("action", choices=("add",))
    s.add_argument("file")
    s.add_argument("--name")
    s = add("catalog", cmd_catalog, "application catalog")
    s.add_argument("action", choices=("add", "list"))
    s.add_argument("manifest", nargs="?")
    s.add_argument("--installer")
    s = add("policy", cmd_policy, "device policies")
    s.add_argument("action", choices=("publish", "list"))
    s.add_argument("file", nargs="?")
    s = add("device", cmd_device, "devices")
    s.add_argument("action", choices=("list", "retire"))
    s.add_argument("device_id", nargs="?")
    s = add("events", cmd_events, "audit trail")
    s.add_argument("action", choices=("list", "verify"))
    s.add_argument("--limit", type=int, default=50)
    add("ca-certificate", cmd_ca, "print the CA certificate devices pin")
    add("sweep", cmd_sweep, "mark silent devices offline, expire commands")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    settings = Settings.load(Path(args.config))
    logging.basicConfig(stream=sys.stderr, format="%(levelname)s: %(message)s",
                        level={"debug": logging.DEBUG, "warning": logging.WARNING, "error": logging.ERROR}.get(
                            settings["LOG_LEVEL"], logging.INFO))
    for needed, action in (("name", ("add", "disable")), ("device_id", ("retire",)), ("manifest", ("add",)),
                           ("file", ("publish",))):
        if getattr(args, "action", None) in action and hasattr(args, needed) and not getattr(args, needed):
            parser.error(f"{args.command} {args.action} needs {needed.replace('_', ' ')}")
    try:
        return args.handler(args, settings)
    except ServiceError as exc:
        sys.stderr.write(f"boswas-cp: {exc.code}: {exc.message}\n")
        for detail in exc.details[:10]:
            sys.stderr.write(f"  {detail}\n")
        return 1
