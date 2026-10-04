"""Persistent local device identity (/var/lib/boswas/agent/identity.json).

The device ID is a random UUID version 4 from the kernel CSPRNG, created the
first time the agent starts and kept for the life of the installation:

  * never regenerated on boot, and never silently replaced: a damaged
    identity file stops the agent (ERROR) until an administrator runs
    `boswas-device identity reset`, which deliberately creates a new device;
  * never derived from the hostname, a MAC address, a disk or board serial
    number or a user name;
  * not a secret and not a credential: after enrollment the device
    authenticates with its certificate, never with the ID alone.

Reinstallation wipes /var/lib, so a reinstalled device gets a new ID. To keep
an ID across a reinstall, an administrator pre-provisions it as DEVICE_ID in
/etc/boswas/device.conf (for example through the installer preseed) before
the agent's first start; the device must enroll again in either case,
because its private key is gone.

Live sessions keep the identity in RAM: it is marked ephemeral and
disappears at shutdown.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from . import config
from .commands import format_time, utc_now
from .device_identity import generate_device_id, is_valid_device_id
from .errors import IdentityError
from .paths import AgentPaths
from .storage import ensure_dir, read_bytes

IDENTITY_SCHEMA = "boswas-device-identity/1"
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    created_at: str
    source: str                 # generated | provisioned
    ephemeral: bool

    def to_dict(self) -> dict:
        return {"schema": IDENTITY_SCHEMA, "device_id": self.device_id, "created_at": self.created_at,
                "source": self.source, "ephemeral": self.ephemeral}


def is_live_session(paths: AgentPaths) -> bool:
    if (paths.root / "run/live").is_dir():
        return True
    cmdline = read_bytes(paths.root / "proc/cmdline", 65536) or b""
    return b"boot=live" in cmdline.split()


class IdentityStore:
    def __init__(self, paths: AgentPaths):
        self.paths = paths

    def load(self) -> DeviceIdentity | None:
        """The persisted identity, None if there is none yet; IdentityError if damaged."""
        if not os.path.lexists(self.paths.identity):
            return None
        data = read_bytes(self.paths.identity, 65536)
        try:
            doc = json.loads(data.decode("utf-8")) if data is not None else None
        except (UnicodeDecodeError, ValueError):
            doc = None
        if not isinstance(doc, dict) or doc.get("schema") != IDENTITY_SCHEMA:
            raise IdentityError(f"{self.paths.identity} is damaged; refusing to replace the device identity "
                                "silently (an administrator can run 'boswas-device identity reset')")
        if not is_valid_device_id(doc.get("device_id")) or not _TIME_RE.fullmatch(str(doc.get("created_at"))) \
                or doc.get("source") not in ("generated", "provisioned") or not isinstance(doc.get("ephemeral"), bool):
            raise IdentityError(f"{self.paths.identity} does not contain a valid device identity")
        return DeviceIdentity(doc["device_id"], doc["created_at"], doc["source"], doc["ephemeral"])

    def ensure(self) -> DeviceIdentity:
        """The identity, created on first use. Safe against concurrent creation."""
        existing = self.load()
        if existing is not None:
            return existing
        provisioned = ""
        conf = read_bytes(self.paths.device_conf, config.MAX_CONF_BYTES)
        if conf is not None:
            provisioned = config.parse(conf.decode("utf-8", errors="replace")).get("DEVICE_ID", "")
        if provisioned and not is_valid_device_id(provisioned):
            raise IdentityError("DEVICE_ID in device.conf is not a valid device ID (random UUID version 4)")
        identity = DeviceIdentity(device_id=provisioned or generate_device_id(), created_at=format_time(utc_now()),
                                  source="provisioned" if provisioned else "generated",
                                  ephemeral=is_live_session(self.paths))
        return self._create(identity)

    def _create(self, identity: DeviceIdentity) -> DeviceIdentity:
        ensure_dir(self.paths.state_dir, 0o755)
        tmp = self.paths.state_dir / f".identity.{secrets.token_hex(6)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(identity.to_dict(), fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, self.paths.identity)     # fails if another process won the race
            except FileExistsError:
                return self.load()
        finally:
            os.unlink(tmp)
        return identity

    def reset(self) -> DeviceIdentity:
        """Deliberately create a new device identity (administrator action)."""
        try:
            Path(self.paths.identity).unlink()
        except FileNotFoundError:
            pass
        identity = DeviceIdentity(device_id=generate_device_id(), created_at=format_time(utc_now()),
                                  source="generated", ephemeral=is_live_session(self.paths))
        return self._create(identity)
