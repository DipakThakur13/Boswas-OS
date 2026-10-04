"""Compatibility database: layered catalog of manifests.

Layers, highest precedence first (paths.CATALOG_LAYERS):

  managed  /var/lib/boswas/compat/manifests   synchronised from the Control
                                              Plane (reserved, Milestone 2+)
  local    /etc/boswas/compat/manifests       device administrator
  system   /usr/share/boswas/compat/manifests shipped by boswas-compat

For an application ID the highest layer wins, except that "blocked" in any
layer always wins: no layer can unblock an application another layer blocks.
Invalid manifests are skipped and reported, never half-applied.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from . import manifest as mf
from . import paths
from .errors import ManifestError


class Catalog:
    def __init__(self, layers: list[tuple[str, Path]] | None = None):
        if layers is None:
            layers = [(name, paths.system_path(path)) for name, path in paths.CATALOG_LAYERS]
        self.layers = layers
        self.problems: list[str] = []
        self._by_layer: dict[str, dict[str, mf.Manifest]] = {}
        self._load()

    def _load(self) -> None:
        for name, directory in self.layers:
            found: dict[str, mf.Manifest] = {}
            try:
                files = sorted(p for p in directory.iterdir() if p.suffix == ".json")
            except OSError:
                files = []
            for path in files:
                try:
                    if not path.is_file():
                        continue
                    manifest = mf.load(path, layer=name)
                except ManifestError as exc:
                    self.problems.append(str(exc))
                    continue
                found[manifest.id] = manifest
            self._by_layer[name] = found

    def get(self, app_id: str) -> mf.Manifest | None:
        """Effective manifest for an application ID."""
        candidates = [self._by_layer[name][app_id] for name, _ in self.layers
                      if app_id in self._by_layer.get(name, {})]
        if not candidates:
            return None
        for candidate in candidates:
            if candidate.status == "blocked":
                if candidate is candidates[0]:
                    return candidate
                # A higher layer may not unblock what a lower layer blocks.
                return replace(candidates[0], status="blocked",
                               notes=f"blocked by the {candidate.layer} catalog")
        return candidates[0]

    def all(self) -> list[mf.Manifest]:
        ids = sorted({app_id for layer in self._by_layer.values() for app_id in layer})
        return [m for m in (self.get(app_id) for app_id in ids) if m is not None]

    def by_installer_sha256(self, sha256: str) -> list[mf.Manifest]:
        """Effective manifests whose pinned installer has this SHA-256.

        Any layer's pin counts, so a blocked installer is found even when a
        higher layer redefines the application.
        """
        ids = {m.id for layer in self._by_layer.values() for m in layer.values()
               if m.installer.sha256 == sha256}
        return [m for m in (self.get(app_id) for app_id in sorted(ids)) if m is not None]
