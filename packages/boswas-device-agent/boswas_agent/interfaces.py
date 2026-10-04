"""Interfaces of the device agent.

  ControlPlaneClient   HTTPS + mutual TLS to the Control Plane (device API v1);
                       client.HttpsControlPlaneClient implements it
  CredentialStore      device key pair and certificate; the private key never
                       leaves the store (credentials.FileCredentialStore: a
                       root-only directory; TPM-backed later)
  PolicyVerifier       verifies signed policy documents before anything is
                       applied (policy_store.PolicyStore)
  InventoryCollector   one source of inventory facts
  CommandHandler       executes one CommandType

Device identity is the device's certificate. It is never the hostname, the
IP address or a MAC address, and it is independent of any user identity
(see user_identity.py for the future Boswas ID boundary).
"""

from __future__ import annotations

import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .errors import NotEnrolled
from .models import EnrollmentRequest


@dataclass(frozen=True)
class EnrollmentResult:
    device_id: str
    certificate_pem: str           # issued device certificate (public)
    control_plane_url: str
    policy_version: str | None
    profile: str | None
    policy_public_key_pem: str = ""  # pinned to verify signed policies
    heartbeat_seconds: int = 300


@dataclass(frozen=True)
class HeartbeatResponse:
    accepted: bool
    policy_version_available: str | None = None
    commands_pending: int = 0
    next_heartbeat_seconds: int = 300
    inventory_requested: bool = False


@dataclass(frozen=True)
class SignedPolicy:
    version: str
    document: bytes                # canonical JSON
    signature: bytes               # detached signature over document
    key_id: str


@dataclass(frozen=True)
class VerifiedPolicy:
    version: str
    content: dict
    key_id: str


class ControlPlaneClient(ABC):
    """Device side of the Control Plane device API (v1)."""

    @abstractmethod
    def enroll(self, request: EnrollmentRequest) -> EnrollmentResult:
        """POST /api/v1/enroll (one-time enrollment token + CSR)."""

    @abstractmethod
    def heartbeat(self, heartbeat: dict) -> HeartbeatResponse:
        """POST /api/v1/devices/{id}/heartbeat."""

    @abstractmethod
    def send(self, kind: str, document: dict) -> None:
        """Deliver an outbox document: status, compliance, inventory, event or command result."""

    @abstractmethod
    def fetch_policy(self) -> dict | None:
        """The device's current signed policy envelope, or None if it has none."""

    @abstractmethod
    def fetch_commands(self) -> list[dict]:
        """Pending commands for this device (marked SENT by the Control Plane)."""

    @abstractmethod
    def acknowledge(self, command_id: str, status: str) -> None:
        """Report that a command was received (ACKNOWLEDGED) or started (RUNNING)."""

    @abstractmethod
    def download_artifact(self, sha256: str, destination: Path, max_bytes: int) -> None:
        """Download an installer referenced by a command; the caller verifies it."""


class OfflineControlPlaneClient(ControlPlaneClient):
    """Client for unenrolled devices: every call fails with NotEnrolled.

    An unenrolled Boswas OS device is fully usable offline and contacts no
    backend; this is the default until enrollment.
    """

    def _offline(self, *_args, **_kwargs):
        raise NotEnrolled("this device is not enrolled; no Control Plane is configured")

    enroll = heartbeat = send = fetch_policy = fetch_commands = acknowledge = download_artifact = _offline
    # Milestone 1 name, kept for callers of the original interface.
    report_compliance = _offline


class CredentialStore(ABC):
    @abstractmethod
    def has_credential(self) -> bool:
        """A device certificate and its private key exist."""

    @abstractmethod
    def certificate_path(self) -> Path | None:
        """Public certificate."""

    @abstractmethod
    def create_csr(self, device_id: str) -> str:
        """Generate a new key pair inside the store and return a PEM CSR."""

    @abstractmethod
    def store_certificate(self, certificate_pem: str) -> Path:
        """Install the certificate issued at enrollment."""

    @abstractmethod
    def tls_context(self, control_plane_ca: Path) -> ssl.SSLContext:
        """Client TLS context (mutual TLS); the private key stays in the store."""


class PolicyVerifier(ABC):
    @abstractmethod
    def verify(self, policy: SignedPolicy) -> VerifiedPolicy:
        """Verify signature and integrity; raise PolicyVerificationError otherwise."""


class InventoryCollector(ABC):
    name: str = "collector"

    @abstractmethod
    def collect(self) -> dict:
        """Inventory facts. Must stay within the documented privacy limits."""


class CommandHandler(ABC):
    command_type: object

    @abstractmethod
    def handle(self, command) -> object:
        """Execute one command; never raise for an expected failure."""
