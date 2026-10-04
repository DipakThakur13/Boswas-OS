"""Device side of managed policy and the managed compatibility catalog.

PolicyStore applies signed Control Plane policies. A policy is applied only
if it verifies against the key pinned at enrollment, is a valid document,
is not older than the applied one, and renders to a WinCompat policy that
boswas-compat itself parses without a single problem. Otherwise nothing
changes: a valid local security policy is never replaced by malformed or
unverified remote data.

ManagedCatalog owns /var/lib/boswas/compat/manifests (the "managed" layer
of the WinCompat catalog): manifests of applications the Control Plane
installs, each validated by boswas-compat's own manifest validator first.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from boswas_compat import manifest as compat_manifest
from boswas_compat import policy as compat_policy

from . import policydoc
from .commands import format_time, parse_time, utc_now
from .credentials import FileCredentialStore
from .errors import PolicyVerificationError
from .paths import AgentPaths
from .storage import atomic_write, ensure_dir, read_json, remove_file, write_json


class PolicyStore:
    def __init__(self, paths: AgentPaths, credentials: FileCredentialStore):
        self.paths = paths
        self.credentials = credentials
        self.current_path = paths.policy_dir / "current.json"

    def current(self) -> dict | None:
        doc = read_json(self.current_path)
        if not isinstance(doc, dict) or not isinstance(doc.get("document"), dict):
            return None
        return doc

    def summary(self) -> dict:
        cur = self.current()
        if cur is None:
            return {"managed": False, "version": None, "name": None, "sequence": None, "applied_at": None}
        d = cur["document"]
        return {"managed": True, "version": d.get("version"), "name": d.get("name"), "sequence": d.get("sequence"),
                "issued_at": d.get("issued_at"), "applied_at": cur.get("applied_at"),
                "description": d.get("description")}

    def version(self) -> str | None:
        cur = self.current()
        return cur["document"].get("version") if cur else None

    def agent_settings(self) -> dict | None:
        cur = self.current()
        return dict(cur["document"]["agent"]) if cur else None

    def updates_setting(self) -> str | None:
        cur = self.current()
        return cur["document"]["updates"]["agent_updates"] if cur else None

    def verify(self, envelope: dict) -> dict:
        """The verified, valid policy document of an envelope (nothing is applied)."""
        pinned = self.credentials.policy_public_key()
        if not pinned:
            raise PolicyVerificationError("no policy key is pinned (the device is not enrolled)")
        return policydoc.open_envelope(envelope, pinned)

    def apply(self, envelope: dict) -> tuple[dict, bool]:
        """Verify and apply an envelope. Returns (document, changed)."""
        doc = self.verify(envelope)
        cur = self.current()
        if cur is not None:
            old = cur["document"]
            if old.get("version") == doc["version"] and policydoc.canonical_json(old) == policydoc.canonical_json(doc):
                return doc, False
            if old.get("name") == doc["name"] and doc["sequence"] <= old.get("sequence", 0):
                raise PolicyVerificationError(f"policy {doc['version']} is older than the applied {old.get('version')}")
            if parse_time(doc["issued_at"]) < parse_time(old["issued_at"]):
                raise PolicyVerificationError("policy was issued before the applied policy (rollback refused)")
        text = policydoc.render_compat_policy(doc)
        self._check_rendering(text)
        ensure_dir(self.paths.compat_dir, 0o755)
        atomic_write(self.paths.managed_policy, text, 0o644)
        ensure_dir(self.paths.policy_dir, 0o700)
        write_json(self.current_path, {"envelope": envelope, "document": doc,
                                       "applied_at": format_time(utc_now())}, 0o600)
        return doc, True

    @staticmethod
    def _check_rendering(text: str) -> None:
        """The rendered file must parse in boswas-compat exactly as intended."""
        with tempfile.TemporaryDirectory(prefix="boswas-policy-") as tmp:
            path = Path(tmp) / "policy.conf"
            path.write_text(text)
            parsed = compat_policy.Policy.load(path)
        if parsed.problems or not parsed.require_apparmor:
            raise PolicyVerificationError(f"rendered policy rejected by boswas-compat: {list(parsed.problems)[:1]}")

    def clear(self) -> None:
        """Back to the local policy (unenroll)."""
        remove_file(self.paths.managed_policy)
        remove_file(self.current_path)


class ManagedCatalog:
    def __init__(self, paths: AgentPaths):
        self.paths = paths

    def install(self, manifest_doc: dict) -> Path:
        manifest = compat_manifest.from_document(manifest_doc, layer="managed")
        ensure_dir(self.paths.compat_dir, 0o755)
        ensure_dir(self.paths.managed_manifests, 0o755)
        path = self.paths.managed_manifests / f"{manifest.id}.json"
        atomic_write(path, json.dumps(manifest_doc, indent=2, sort_keys=True) + "\n", 0o644)
        # Read it back through the loader boswas-winapp uses (file name, size, digest).
        compat_manifest.load(path, layer="managed")
        return path

    def remove(self, app_id: str) -> bool:
        if not compat_manifest.valid_id(app_id):
            return False
        return remove_file(self.paths.managed_manifests / f"{app_id}.json")

    def ids(self) -> list[str]:
        try:
            names = os.listdir(self.paths.managed_manifests)
        except FileNotFoundError:
            return []
        return sorted(n[:-5] for n in names if n.endswith(".json") and compat_manifest.valid_id(n[:-5]))

    def clear(self) -> None:
        for app_id in self.ids():
            self.remove(app_id)
