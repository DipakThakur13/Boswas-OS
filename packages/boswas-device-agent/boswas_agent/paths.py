"""Filesystem contract of the device agent (docs/device-management/README.md).

  /etc/boswas/device.conf            configuration (administrator; never secrets)
  /var/lib/boswas/agent/             agent state, root-owned, 0755
    identity.json                    device identity (public, 0644)
    status.json                      device and agent status (public, 0644)
    inventory.json                   last inventory (public, 0644; no user names)
    events.jsonl                     local event log (0640)
    commands.json                    ledger of executed remote commands (0600)
    outbox/                          messages waiting for the Control Plane (0700)
    credentials/                     device key and certificate, pinned keys (0700)
    policy/                          applied signed policy (0700)
    artifacts/                       verified installers for remote installs (0755)
  /var/lib/boswas/compat/            managed WinCompat layer (policy.conf, manifests/)
  /run/boswas-agent/agent.sock       local management API

Tests point everything at a temporary root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentPaths:
    root: Path = Path("/")

    def _p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    @property
    def device_conf(self) -> Path:
        return self._p("/etc/boswas/device.conf")

    @property
    def release(self) -> Path:
        return self._p("/usr/lib/boswas/release")

    @property
    def image_info(self) -> Path:
        return self._p("/usr/lib/boswas/image-info")

    @property
    def state_dir(self) -> Path:
        return self._p("/var/lib/boswas/agent")

    @property
    def identity(self) -> Path:
        return self.state_dir / "identity.json"

    @property
    def status(self) -> Path:
        return self.state_dir / "status.json"

    @property
    def inventory(self) -> Path:
        return self.state_dir / "inventory.json"

    @property
    def events(self) -> Path:
        return self.state_dir / "events.jsonl"

    @property
    def ledger(self) -> Path:
        return self.state_dir / "commands.json"

    @property
    def outbox(self) -> Path:
        return self.state_dir / "outbox"

    @property
    def credentials(self) -> Path:
        return self.state_dir / "credentials"

    @property
    def policy_dir(self) -> Path:
        return self.state_dir / "policy"

    @property
    def artifacts(self) -> Path:
        return self.state_dir / "artifacts"

    @property
    def compat_dir(self) -> Path:
        return self._p("/var/lib/boswas/compat")

    @property
    def managed_policy(self) -> Path:
        return self.compat_dir / "policy.conf"

    @property
    def managed_manifests(self) -> Path:
        return self.compat_dir / "manifests"

    @property
    def runtime_dir(self) -> Path:
        return self._p("/run/boswas-agent")

    @property
    def socket(self) -> Path:
        return self.runtime_dir / "agent.sock"

    @property
    def live_marker(self) -> Path:
        return self._p("/run/live")
