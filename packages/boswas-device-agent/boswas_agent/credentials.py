"""File-based device credential store (/var/lib/boswas/agent/credentials, 0700 root).

  device.key            EC P-256 private key, created on the device (0600);
                        never leaves this directory, never logged
  device.crt            certificate issued by the Control Plane at enrollment
  policy-signing.pub    the Control Plane's policy key, pinned at enrollment

The key is generated inside the store with `openssl req`, and a certificate
is only installed if its subject is this device's ID and its public key is
this device's key. A TPM-backed store can replace this class later
(interfaces.CredentialStore).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import ssl
import subprocess
import tempfile
from pathlib import Path

from .errors import CredentialError
from .interfaces import CredentialStore
from .storage import atomic_write, ensure_dir, read_bytes

OPENSSL = "/usr/bin/openssl"
_CERT_RE = re.compile(r"-----BEGIN CERTIFICATE-----\s+([A-Za-z0-9+/=\s]+?)\s+-----END CERTIFICATE-----")
_PUBKEY_RE = re.compile(r"^-----BEGIN PUBLIC KEY-----\n[A-Za-z0-9+/=\n]+-----END PUBLIC KEY-----\n?$")
MAX_PEM = 64 * 1024


def _openssl(args: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([OPENSSL, *args], input=input_bytes, capture_output=True, timeout=60, check=False,
                              env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError) as exc:
        raise CredentialError(f"openssl could not run: {type(exc).__name__}") from exc


def certificate_der(pem: str) -> bytes:
    m = _CERT_RE.search(pem or "")
    if not m:
        raise CredentialError("not a PEM certificate")
    try:
        return base64.b64decode("".join(m.group(1).split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CredentialError("damaged PEM certificate") from exc


def fingerprint(pem: str) -> str:
    """SHA-256 fingerprint of a certificate (lower-case hex)."""
    return hashlib.sha256(certificate_der(pem)).hexdigest()


class FileCredentialStore(CredentialStore):
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.key = self.directory / "device.key"
        self.cert = self.directory / "device.crt"
        self.pending_key = self.directory / "device.key.new"
        self.policy_key = self.directory / "policy-signing.pub"

    def _prepare(self) -> None:
        ensure_dir(self.directory.parent, 0o755)
        ensure_dir(self.directory, 0o700)

    def has_credential(self) -> bool:
        return self.key.is_file() and self.cert.is_file()

    def certificate_path(self) -> Path | None:
        return self.cert if self.cert.is_file() else None

    def certificate_pem(self) -> str | None:
        data = read_bytes(self.cert, MAX_PEM)
        return data.decode("ascii", errors="replace") if data else None

    def certificate_fingerprint(self) -> str | None:
        pem = self.certificate_pem()
        try:
            return fingerprint(pem) if pem else None
        except CredentialError:
            return None

    def create_csr(self, device_id: str) -> str:
        """A new key pair (kept as the pending key until a certificate arrives) and its CSR."""
        self._prepare()
        self.pending_key.unlink(missing_ok=True)
        old = os.umask(0o077)
        try:
            proc = _openssl(["req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                             "-keyout", str(self.pending_key), "-subj", f"/O=Boswas OS device/CN={device_id}",
                             "-outform", "PEM"])
        finally:
            os.umask(old)
        if proc.returncode != 0 or b"BEGIN CERTIFICATE REQUEST" not in proc.stdout:
            self.pending_key.unlink(missing_ok=True)
            raise CredentialError("creating the device key and certificate request failed")
        os.chmod(self.pending_key, 0o600)
        return proc.stdout.decode("ascii")

    def store_certificate(self, certificate_pem: str, device_id: str | None = None) -> Path:
        """Install the certificate issued for the pending key; refuse anything else."""
        if not self.pending_key.is_file():
            raise CredentialError("no pending device key: request a certificate first")
        if len(certificate_pem) > MAX_PEM:
            raise CredentialError("certificate too large")
        certificate_der(certificate_pem)
        with tempfile.TemporaryDirectory(prefix="boswas-cert-") as tmp:
            cert = Path(tmp) / "device.crt"
            cert.write_text(certificate_pem)
            subject = _openssl(["x509", "-in", str(cert), "-noout", "-subject", "-nameopt", "RFC2253"])
            cert_key = _openssl(["x509", "-in", str(cert), "-noout", "-pubkey"])
            own_key = _openssl(["pkey", "-in", str(self.pending_key), "-pubout"])
        if subject.returncode != 0 or cert_key.returncode != 0 or own_key.returncode != 0:
            raise CredentialError("the issued certificate could not be read")
        if device_id is not None and f"CN={device_id}" not in subject.stdout.decode("ascii", errors="replace"):
            raise CredentialError("the issued certificate is not for this device")
        if cert_key.stdout.strip() != own_key.stdout.strip():
            raise CredentialError("the issued certificate does not match this device's key")
        os.replace(self.pending_key, self.key)
        atomic_write(self.cert, certificate_pem, 0o644)
        return self.cert

    def pin_policy_key(self, public_pem: str) -> None:
        if not _PUBKEY_RE.fullmatch(public_pem.replace("\r\n", "\n")):
            raise CredentialError("not a PEM public key")
        self._prepare()
        atomic_write(self.policy_key, public_pem, 0o644)

    def policy_public_key(self) -> str | None:
        data = read_bytes(self.policy_key, MAX_PEM)
        return data.decode("ascii", errors="replace") if data else None

    def tls_context(self, control_plane_ca: Path) -> ssl.SSLContext:
        """Verifies the Control Plane against its pinned CA; presents the device certificate if enrolled."""
        try:
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(control_plane_ca))
        except (OSError, ssl.SSLError) as exc:
            raise CredentialError(f"cannot load the Control Plane CA {control_plane_ca}") from exc
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if self.has_credential():
            try:
                ctx.load_cert_chain(str(self.cert), str(self.key))
            except (OSError, ssl.SSLError) as exc:
                raise CredentialError("the device certificate or key cannot be loaded") from exc
        return ctx

    def clear(self) -> None:
        """Forget the device's credentials (unenroll)."""
        for path in (self.key, self.cert, self.pending_key, self.policy_key):
            path.unlink(missing_ok=True)
