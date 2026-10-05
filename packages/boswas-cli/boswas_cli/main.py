"""`boswas` command-line entry point.

Exit codes (stable, documented in docs/administration/cli.md):
  0   success; for status commands: no check FAILed
  1   one or more posture checks FAILed
  2   usage error
  69  command not available (planned for a later milestone, or its package
      is not installed)
  70  internal error
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from . import JSON_SCHEMA, __version__, checks, identity, system

EXIT_OK, EXIT_CHECK_FAILED, EXIT_USAGE, EXIT_UNAVAILABLE, EXIT_SOFTWARE = 0, 1, 2, 69, 70


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _emit(args, command: str, payload: dict, text: str) -> None:
    if args.json:
        doc = {"schema": JSON_SCHEMA, "command": command, "generated_at": _now(), **payload}
        json.dump(doc, sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(text.rstrip("\n") + "\n")


def _rows(pairs: list[tuple[str, object]]) -> str:
    width = max(len(k) for k, _ in pairs) + 2
    return "\n".join(f"{(k + ':').ljust(width)}{v}" for k, v in pairs)


def _compliance_line(summary: dict) -> str:
    return (f"{summary['state']} ({summary['pass']} pass, {summary['warn']} warn, "
            f"{summary['fail']} fail, {summary['unknown']} unknown)")


def _checks_table(results: list[checks.Check]) -> str:
    width = max(len(c.title) for c in results) + 2
    lines = [f"  {'CHECK'.ljust(width)}{'STATUS':<9}DETAIL"]
    lines += [f"  {c.title.ljust(width)}{c.status:<9}{c.detail}" for c in results]
    return "\n".join(lines)


# --- commands -----------------------------------------------------------------

def cmd_version(args) -> int:
    _emit(args, "version", {"version": __version__}, f"boswas {__version__}")
    return EXIT_OK


def cmd_info(args) -> int:
    ident = identity.os_identity()
    device = identity.device_config()
    results = checks.run_checks()
    summary = checks.compliance(results)
    build = ident["build"]
    secure_boot = next(c for c in results if c.id == "secure-boot")
    boot = ident["boot_mode"] + (f" (Secure Boot: {secure_boot.detail})" if ident["boot_mode"] == "UEFI" else "")
    text = ident["name"] + "\n" + _rows([
        ("Version", f"{ident['version']} ({ident['version_id']})"),
        ("Build", build["id"] or "unknown"),
        ("Channel", ident["channel"]),
        ("Package base", f"{ident['base']['name']} {ident['base']['version']} ({ident['base']['codename']})"),
        ("Architecture", ident["architecture"]),
        ("Kernel", ident["kernel"]),
        ("Desktop", ident["desktop"]),
        ("Hostname", ident["hostname"]),
        ("Boot", boot),
        ("Session", "live (not installed)" if ident["live_session"] else "installed"),
        ("Device ID", device["device_id"] or "not assigned"),
        ("Enrollment", device["enrollment_state"] or "unenrolled"),
        ("Compliance", _compliance_line(summary)),
    ])
    payload = {"os": ident, "device": device, "compliance": summary}
    _emit(args, "info", payload, text)
    return EXIT_OK


def _status(args, command: str, selected) -> int:
    results = checks.run_checks(selected)
    summary = checks.compliance(results)
    header = (f"Boswas OS {command.replace('-', ' ')} - {identity.os_identity()['hostname']} - {_now()}\n"
              f"Compliance: {_compliance_line(summary)}\n"
              f"Basis: {summary['basis']}\n")
    payload = {"compliance": summary, "checks": [c.to_dict() for c in results]}
    _emit(args, command, payload, header + "\n" + _checks_table(results))
    return EXIT_CHECK_FAILED if summary["fail"] else EXIT_OK


def cmd_status(args) -> int:
    return _status(args, "status", checks.ALL_CHECKS)


def cmd_security_status(args) -> int:
    return _status(args, "security-status", checks.SECURITY_CHECKS)


def cmd_device_status(args) -> int:
    device = identity.device_config()
    hardware = identity.hardware_summary()
    agent = identity.agent_status()
    agent_rows = [("Device state", agent["state"] or "unknown"),
                  ("Connection", agent["connection"] or "unknown"),
                  ("Agent", f"{agent['agent_version'] or 'unknown'} (status of {agent['updated_at'] or 'unknown'})")] \
        if agent else [("Agent", "not running yet")]
    text = "Boswas device\n" + _rows([
        ("Device ID", device["device_id"] or "not assigned"),
        *agent_rows,
        ("Tenant ID", device["tenant_id"] or "not assigned"),
        ("Enrollment", device["enrollment_state"] or "unenrolled"),
        ("Control plane", device["control_plane_url"] or "not configured"),
        ("Policy version", device["policy_version"] or "none"),
        ("Profile", device["device_profile"] or "none"),
        ("Certificate", device["device_certificate"] or "none"),
        ("Vendor", hardware["vendor"] or "unknown"),
        ("Model", hardware["product"] or "unknown"),
        ("Firmware", hardware["firmware_version"] or "unknown"),
        ("CPU", hardware["cpu"] or "unknown"),
        ("Memory", f"{hardware['memory_gib']} GiB" if hardware["memory_gib"] else "unknown"),
        ("Boot mode", hardware["boot_mode"]),
        ("TPM", f"{hardware['tpm_version']}.x" if hardware["tpm_version"] else "not detected"),
    ])
    _emit(args, "device-status", {"device": device, "hardware": hardware, "agent": agent}, text)
    return EXIT_OK


def cmd_policy_status(args) -> int:
    device = identity.device_config()
    state = "managed" if device["policy_version"] else "unmanaged"
    agent = system.package_version("boswas-device-agent")
    payload = {
        "policy": {
            "state": state,
            "policy_version": device["policy_version"],
            "engine": "boswas-device-agent" if agent else None,
            "note": ("Signed Control Plane policies are verified and applied by the device agent (boswas-device "
                     "policy); without enrollment the local WinCompat policy and the boswas-security baseline apply."),
        }
    }
    text = "Boswas policy\n" + _rows([
        ("State", state),
        ("Policy version", device["policy_version"] or "none (local policy)"),
        ("Engine", f"boswas-device-agent {agent}" if agent else "boswas-device-agent not installed"),
        ("Local baseline", "boswas-security " + (system.package_version("boswas-security") or "not installed")),
    ])
    _emit(args, "policy-status", payload, text)
    return EXIT_OK


def cmd_update_status(args) -> int:
    ident = identity.os_identity()
    update_conf = system.load_env(identity.UPDATE_CONF)
    check = checks.updates()
    age = checks.package_lists_age_days()
    payload = {
        "update": {
            "channel": ident["channel"],
            "repository": update_conf.get("REPOSITORY") or None,
            "automatic_security_updates": checks.unattended_upgrades_enabled(),
            "package_lists_age_days": None if age is None else round(age, 1),
            "status": check.status,
            "detail": check.detail,
        }
    }
    text = "Boswas updates\n" + _rows([
        ("Channel", ident["channel"]),
        ("Repository", update_conf.get("REPOSITORY") or "upstream package archive (Boswas repository not yet provisioned)"),
        ("Status", f"{check.status} - {check.detail}"),
    ])
    _emit(args, "update-status", payload, text)
    return EXIT_OK


WINAPP = "/usr/bin/boswas-winapp"


def cmd_winapp(args) -> int:
    """`boswas winapp ...` runs boswas-winapp (package boswas-compat) when installed."""
    tool = system.sysroot_path(WINAPP)
    if not os.access(tool, os.X_OK):
        return _unavailable("Windows application management (boswas winapp)",
                            "install the boswas-compat package")(args)
    argv = [str(tool), *(["--json"] if args.json else []), *args.winapp_args]
    sys.stdout.flush()
    return subprocess.run(argv, check=False).returncode


def _unavailable(feature: str, phase: str):
    def handler(args) -> int:
        message = f"{feature} is not available ({phase})."
        if args.json:
            _emit(args, "unavailable", {"error": "unavailable", "message": message}, message)
        else:
            sys.stderr.write(message + "\n")
        return EXIT_UNAVAILABLE
    return handler


# --- argument parsing -----------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable JSON output")

    parser = argparse.ArgumentParser(
        prog="boswas", parents=[common],
        description="Boswas OS administration and status tool (read-only in v1 alpha).")
    parser.add_argument("--version", action="version", version=f"boswas {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name, handler, help_text, parent=sub):
        p = parent.add_parser(name, parents=[common], help=help_text, description=help_text)
        p.set_defaults(handler=handler)
        return p

    add("info", cmd_info, "show OS identity, build and device summary")
    add("status", cmd_status, "run all local posture checks")
    add("version", cmd_version, "show the boswas CLI version")

    for group, help_text, actions in (
        ("device", "device identity and hardware", {"status": cmd_device_status}),
        ("policy", "device policy state", {"status": cmd_policy_status}),
        ("update", "update channel and state", {"status": cmd_update_status}),
        ("security", "security baseline checks", {"status": cmd_security_status}),
        ("app", "managed applications (Boswas Store)",
         {"list": _unavailable("Application management (boswas app)", "planned: Boswas Store, Milestone 6")}),
    ):
        gp = sub.add_parser(group, parents=[common], help=help_text, description=help_text)
        gsub = gp.add_subparsers(dest="action", metavar="<action>", required=True)
        for action, handler in actions.items():
            add(action, handler, f"{group} {action}", parent=gsub)

    wp = sub.add_parser("winapp", parents=[common], help="Windows applications (runs boswas-winapp)",
                        description="Windows applications: passes its arguments to boswas-winapp.")
    wp.add_argument("winapp_args", nargs=argparse.REMAINDER, metavar="...")
    wp.set_defaults(handler=cmd_winapp)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.handler(args)
    except BrokenPipeError:
        return EXIT_OK
    except Exception as exc:  # report, never dump a traceback with local state
        sys.stderr.write(f"boswas: internal error: {exc}\n")
        return EXIT_SOFTWARE


if __name__ == "__main__":
    sys.exit(main())
