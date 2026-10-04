"""Signed device policy documents, shared by the device agent and the Control Plane.

A policy is a closed JSON document (``boswas-policy/1``) signed with the
Control Plane's Ed25519 policy key and wrapped in an envelope
(``boswas-signed-policy/1``). Devices pin the policy public key at
enrollment and verify, in this order, before anything is applied:

  1. envelope format and key ID (the pinned key)
  2. Ed25519 signature over the exact document bytes
  3. strict document validation (closed fields, types, ranges)
  4. sequence number higher than the applied policy (no rollback)

A policy can never switch off AppArmor confinement (``require_apparmor``
must be true) and can only name the typed commands that exist.

Signing and verification use the ``openssl`` command (Debian package
openssl), so the agent and the Control Plane stay Python standard library
only. Keys and documents are passed through private temporary files, never
on a command line.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from boswas_compat import manifest as compat_manifest

from .commands import CommandType, parse_time
from .errors import PolicyVerificationError

POLICY_SCHEMA = "boswas-policy/1"
ENVELOPE_SCHEMA = "boswas-signed-policy/1"
ALGORITHM = "ed25519"
OPENSSL = "/usr/bin/openssl"
MAX_DOCUMENT_BYTES = 256 * 1024

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
KEY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PEM_RE = re.compile(r"-----BEGIN PUBLIC KEY-----\s+([A-Za-z0-9+/=\s]+?)\s+-----END PUBLIC KEY-----")

COMPAT_STATUSES = tuple(s for s in compat_manifest.STATUSES if s != "blocked")
DEVICES = ("display", "audio", "gpu")
AGENT_UPDATES = ("manual", "managed")


# --- validation ----------------------------------------------------------------------

def _closed(obj: object, keys: set[str], where: str, problems: list[str]) -> dict:
    if not isinstance(obj, dict):
        problems.append(f"{where}: must be an object")
        return {}
    for key in sorted(set(obj) - keys):
        problems.append(f"{where}: unknown key {key!r}")
    for key in sorted(keys - set(obj)):
        problems.append(f"{where}: missing key {key!r}")
    return obj


def _word_list(value: object, allowed, where: str, problems: list[str]) -> None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{where}: must be a list of strings")
        return
    if len(set(value)) != len(value):
        problems.append(f"{where}: duplicate entries")
    bad = [v for v in value if v not in allowed]
    if bad:
        problems.append(f"{where}: unknown value(s) {', '.join(sorted(bad)[:5])}")


def _int(value: object, low: int, high: int, where: str, problems: list[str]) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        problems.append(f"{where}: integer from {low} to {high}")


def _ids(value: object, where: str, problems: list[str]) -> None:
    if not isinstance(value, list) or not all(compat_manifest.valid_id(v) for v in value):
        problems.append(f"{where}: list of application IDs")
    elif len(set(value)) != len(value):
        problems.append(f"{where}: duplicate entries")


def validate_policy(doc: object) -> list[str]:
    """All problems with a policy document (empty list: valid)."""
    problems: list[str] = []
    top = _closed(doc, {"schema", "name", "sequence", "version", "issued_at", "description", "compat", "agent",
                        "updates"}, "policy", problems)
    if not top:
        return problems or ["policy: empty"]
    if top.get("schema") != POLICY_SCHEMA:
        problems.append(f"policy.schema: must be {POLICY_SCHEMA}")
    name, seq = top.get("name"), top.get("sequence")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        problems.append("policy.name: lower-case identifier (max 32)")
    _int(seq, 1, 10 ** 9, "policy.sequence", problems)
    if isinstance(name, str) and isinstance(seq, int) and top.get("version") != f"{name}-{seq}":
        problems.append("policy.version: must be '<name>-<sequence>'")
    try:
        parse_time(top.get("issued_at"))
    except ValueError:
        problems.append("policy.issued_at: UTC timestamp")
    desc = top.get("description")
    if not isinstance(desc, str) or len(desc) > 500 or any(ord(c) < 32 for c in desc):
        problems.append("policy.description: single-line string (max 500)")

    compat = _closed(top.get("compat"), {"allowed_statuses", "unlisted_apps", "unlisted_network",
                                          "unlisted_devices", "unlisted_folders", "require_apparmor",
                                          "max_installer_mb", "blocked_applications", "allowed_applications"},
                     "policy.compat", problems)
    if compat:
        _word_list(compat.get("allowed_statuses"), COMPAT_STATUSES, "policy.compat.allowed_statuses", problems)
        if compat.get("unlisted_apps") not in ("allow", "deny"):
            problems.append("policy.compat.unlisted_apps: allow or deny")
        if not isinstance(compat.get("unlisted_network"), bool):
            problems.append("policy.compat.unlisted_network: true or false")
        _word_list(compat.get("unlisted_devices"), DEVICES, "policy.compat.unlisted_devices", problems)
        _word_list(compat.get("unlisted_folders"), compat_manifest.FOLDERS, "policy.compat.unlisted_folders",
                   problems)
        if compat.get("require_apparmor") is not True:
            problems.append("policy.compat.require_apparmor: must be true (a policy cannot disable confinement)")
        _int(compat.get("max_installer_mb"), 1, 65536, "policy.compat.max_installer_mb", problems)
        _ids(compat.get("blocked_applications"), "policy.compat.blocked_applications", problems)
        if compat.get("allowed_applications") is not None:
            _ids(compat.get("allowed_applications"), "policy.compat.allowed_applications", problems)

    agent = _closed(top.get("agent"), {"heartbeat_seconds", "inventory_seconds", "allowed_commands"},
                    "policy.agent", problems)
    if agent:
        _int(agent.get("heartbeat_seconds"), 30, 86400, "policy.agent.heartbeat_seconds", problems)
        _int(agent.get("inventory_seconds"), 300, 604800, "policy.agent.inventory_seconds", problems)
        _word_list(agent.get("allowed_commands"), [c.value for c in CommandType], "policy.agent.allowed_commands",
                   problems)
    updates = _closed(top.get("updates"), {"agent_updates"}, "policy.updates", problems)
    if updates and updates.get("agent_updates") not in AGENT_UPDATES:
        problems.append("policy.updates.agent_updates: manual or managed")
    return problems


def default_policy(name: str = "default", sequence: int = 1, issued_at: str = "2026-01-01T00:00:00Z",
                   description: str = "Boswas OS default policy") -> dict:
    """Production defaults: catalogued and validated applications only."""
    return {
        "schema": POLICY_SCHEMA, "name": name, "sequence": sequence, "version": f"{name}-{sequence}",
        "issued_at": issued_at, "description": description,
        "compat": {"allowed_statuses": ["approved", "tested"], "unlisted_apps": "deny", "unlisted_network": False,
                   "unlisted_devices": ["display"], "unlisted_folders": [], "require_apparmor": True,
                   "max_installer_mb": 4096, "blocked_applications": [], "allowed_applications": None},
        "agent": {"heartbeat_seconds": 300, "inventory_seconds": 3600,
                  "allowed_commands": [c.value for c in CommandType]},
        "updates": {"agent_updates": "manual"},
    }


def canonical_json(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


# --- keys and signatures (openssl) -------------------------------------------------------------

def _openssl(args: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([OPENSSL, *args], capture_output=True, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError) as exc:
        raise PolicyVerificationError(f"openssl could not run: {type(exc).__name__}") from exc


def public_key_der(public_pem: str) -> bytes:
    m = _PEM_RE.search(public_pem or "")
    if not m:
        raise PolicyVerificationError("not a PEM public key")
    try:
        return base64.b64decode("".join(m.group(1).split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PolicyVerificationError("damaged PEM public key") from exc


def key_id(public_pem: str) -> str:
    """Stable identifier of a public key: SHA-256 of its DER encoding, first 16 bytes."""
    return hashlib.sha256(public_key_der(public_pem)).hexdigest()[:32]


def generate_signing_key(private_path: Path) -> str:
    """Create an Ed25519 private key (mode 0600); return its public key PEM."""
    private_path = Path(private_path)
    old = os.umask(0o077)
    try:
        proc = _openssl(["genpkey", "-algorithm", "ed25519", "-out", str(private_path)])
    finally:
        os.umask(old)
    if proc.returncode != 0:
        raise PolicyVerificationError("openssl genpkey failed")
    os.chmod(private_path, 0o600)
    return public_key_pem(private_path)


def public_key_pem(private_path: Path) -> str:
    proc = _openssl(["pkey", "-in", str(private_path), "-pubout"])
    if proc.returncode != 0:
        raise PolicyVerificationError("cannot read the policy signing key")
    return proc.stdout.decode("ascii")


def sign(data: bytes, private_path: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix="boswas-sign-") as tmp:
        doc, sig = Path(tmp) / "doc", Path(tmp) / "sig"
        doc.write_bytes(data)
        proc = _openssl(["pkeyutl", "-sign", "-rawin", "-inkey", str(private_path), "-in", str(doc), "-out", str(sig)])
        if proc.returncode != 0 or not sig.is_file():
            raise PolicyVerificationError("signing failed")
        return sig.read_bytes()


def verify(data: bytes, signature: bytes, public_pem: str) -> bool:
    if len(signature) != 64:                       # Ed25519 signatures are 64 bytes
        return False
    with tempfile.TemporaryDirectory(prefix="boswas-verify-") as tmp:
        doc, sig, pub = Path(tmp) / "doc", Path(tmp) / "sig", Path(tmp) / "pub.pem"
        doc.write_bytes(data)
        sig.write_bytes(signature)
        pub.write_text(public_pem)
        proc = _openssl(["pkeyutl", "-verify", "-pubin", "-inkey", str(pub), "-rawin", "-in", str(doc),
                         "-sigfile", str(sig)])
        return proc.returncode == 0 and b"Signature Verified Successfully" in proc.stdout


# --- envelopes -----------------------------------------------------------------------------

def make_envelope(doc: dict, private_path: Path, public_pem: str) -> dict:
    """Validate and sign a policy document (Control Plane side)."""
    problems = validate_policy(doc)
    if problems:
        raise PolicyVerificationError(f"invalid policy: {problems[0]}")
    data = canonical_json(doc)
    return {"schema": ENVELOPE_SCHEMA, "algorithm": ALGORITHM, "key_id": key_id(public_pem),
            "document": base64.b64encode(data).decode("ascii"),
            "signature": base64.b64encode(sign(data, private_path)).decode("ascii")}


def open_envelope(envelope: object, trusted_public_pem: str) -> dict:
    """Verify an envelope against the pinned key and return the policy document.

    Raises PolicyVerificationError for anything that is not a correctly
    signed, valid policy; the caller must then keep its current policy.
    """
    if not isinstance(envelope, dict) or set(envelope) != {"schema", "algorithm", "key_id", "document",
                                                             "signature"}:
        raise PolicyVerificationError("not a signed policy envelope")
    if envelope["schema"] != ENVELOPE_SCHEMA or envelope["algorithm"] != ALGORITHM:
        raise PolicyVerificationError("unsupported envelope format or algorithm")
    if not isinstance(envelope["key_id"], str) or envelope["key_id"] != key_id(trusted_public_pem):
        raise PolicyVerificationError("policy signed with an unknown key")
    try:
        data = base64.b64decode(envelope["document"], validate=True)
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise PolicyVerificationError("damaged envelope encoding") from exc
    if len(data) > MAX_DOCUMENT_BYTES:
        raise PolicyVerificationError("policy document too large")
    if not verify(data, signature, trusted_public_pem):
        raise PolicyVerificationError("policy signature is not valid")
    try:
        doc = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PolicyVerificationError("policy document is not JSON") from exc
    problems = validate_policy(doc)
    if problems:
        raise PolicyVerificationError(f"policy document rejected: {problems[0]}")
    if canonical_json(doc) != data:
        raise PolicyVerificationError("policy document is not in canonical form")
    return doc


# --- rendering for boswas-compat ----------------------------------------------------------

def render_compat_policy(doc: dict) -> str:
    """The managed /var/lib/boswas/compat/policy.conf for a verified policy."""
    c = doc["compat"]

    def words(values) -> str:
        return " ".join(values)

    allowed = c["allowed_applications"]
    lines = [
        "# Boswas WinCompat policy - MANAGED by the Boswas device agent.",
        f"# Source: signed Control Plane policy {doc['version']} (issued {doc['issued_at']}).",
        "# Do not edit: the agent replaces this file when a newer verified policy arrives,",
        "# and removes it when the device is unenrolled (the local policy applies again).",
        f'ALLOWED_STATUSES="{words(c["allowed_statuses"])}"',
        f'UNLISTED_APPS="{c["unlisted_apps"]}"',
        f'UNLISTED_NETWORK="{"yes" if c["unlisted_network"] else "no"}"',
        f'UNLISTED_DEVICES="{words(c["unlisted_devices"])}"',
        f'UNLISTED_FOLDERS="{words(c["unlisted_folders"])}"',
        'REQUIRE_APPARMOR="yes"',
        f'MAX_INSTALLER_MB="{c["max_installer_mb"]}"',
        'WINETRICKS_ALLOWED=""',
        f'BLOCKED_APPLICATIONS="{words(c["blocked_applications"])}"',
        # null: no allow list; []: nothing is allowed ("none").
        f'ALLOWED_APPLICATIONS="{"" if allowed is None else (words(allowed) or "none")}"',
    ]
    return "\n".join(lines) + "\n"
