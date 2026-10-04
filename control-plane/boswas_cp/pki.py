"""Control Plane keys and certificates (openssl, private files in the data directory).

  ca/server-ca.{key,crt}       signs the Control Plane's TLS server certificate;
                               devices pin server-ca.crt (CONTROL_PLANE_CA)
  ca/device-ca.{key,crt}       signs device client certificates (mutual TLS)
  tls/server.{key,crt}         the TLS server certificate (serverAuth)
  keys/policy-signing.{key,pub} Ed25519 key that signs device policies

Device certificates are issued from the device's CSR with the subject forced
to "O=Boswas OS device, CN=<device ID>" (whatever the CSR asks for), client
authentication only, and no CA rights. Only EC P-256/P-384 and RSA >= 2048
public keys are accepted. Private keys are created with mode 0600 and never
leave the data directory.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import secrets
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from boswas_agent import policydoc

OPENSSL = "/usr/bin/openssl"
_CERT_RE = re.compile(r"-----BEGIN CERTIFICATE-----\s+([A-Za-z0-9+/=\s]+?)\s+-----END CERTIFICATE-----")
_CSR_RE = re.compile(r"^-----BEGIN CERTIFICATE REQUEST-----\n[A-Za-z0-9+/=\n]+-----END CERTIFICATE REQUEST-----\n?$")
_NAME_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
MAX_CSR = 16 * 1024


class PkiError(Exception):
    pass


def _openssl(*args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([OPENSSL, *args], input=input_bytes, capture_output=True, timeout=60, check=False,
                              env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError) as exc:
        raise PkiError(f"openssl could not run: {type(exc).__name__}") from exc


def certificate_der(pem: str) -> bytes:
    m = _CERT_RE.search(pem or "")
    if not m:
        raise PkiError("not a PEM certificate")
    try:
        return base64.b64decode("".join(m.group(1).split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PkiError("damaged PEM certificate") from exc


def fingerprint_pem(pem: str) -> str:
    return hashlib.sha256(certificate_der(pem)).hexdigest()


def fingerprint_der(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


@dataclass(frozen=True)
class IssuedCertificate:
    pem: str
    fingerprint: str
    expires_at: str


class Pki:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.server_ca_key = self.dir / "ca/server-ca.key"
        self.server_ca_crt = self.dir / "ca/server-ca.crt"
        self.device_ca_key = self.dir / "ca/device-ca.key"
        self.device_ca_crt = self.dir / "ca/device-ca.crt"
        self.server_key = self.dir / "tls/server.key"
        self.server_crt = self.dir / "tls/server.crt"
        self.policy_key = self.dir / "keys/policy-signing.key"
        self.policy_pub = self.dir / "keys/policy-signing.pub"

    # --- setup ---------------------------------------------------------------------------
    def initialize(self, server_names: list[str], *, ca_days: int = 3650, server_days: int = 825) -> None:
        """Create whatever is missing (idempotent)."""
        for sub in ("ca", "tls", "keys"):
            (self.dir / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.server_ca_crt.exists():
            self._make_ca(self.server_ca_key, self.server_ca_crt, "Boswas Control Plane Server CA", ca_days)
        if not self.device_ca_crt.exists():
            self._make_ca(self.device_ca_key, self.device_ca_crt, "Boswas Device CA", ca_days)
        if not self.server_crt.exists():
            self.issue_server_certificate(server_names, server_days)
        if not self.policy_pub.exists():
            pub = policydoc.generate_signing_key(self.policy_key)
            self.policy_pub.write_text(pub)
            os.chmod(self.policy_pub, 0o644)

    def _make_ca(self, key: Path, crt: Path, name: str, days: int) -> None:
        old = os.umask(0o077)
        try:
            proc = _openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-384", "-nodes",
                            "-keyout", str(key), "-out", str(crt), "-subj", f"/O=Boswas Group/CN={name}",
                            "-days", str(days), "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        finally:
            os.umask(old)
        if proc.returncode != 0:
            raise PkiError(f"creating {name} failed: {proc.stderr.decode(errors='replace')[-300:]}")
        os.chmod(key, 0o600)
        os.chmod(crt, 0o644)

    def issue_server_certificate(self, names: list[str], days: int = 825) -> None:
        if not names or not all(_NAME_RE.fullmatch(n) or _is_ip(n) for n in names):
            raise PkiError("server names must be DNS names or IP addresses")
        sans = ",".join(f"IP:{n}" if _is_ip(n) else f"DNS:{n}" for n in names)
        with tempfile.TemporaryDirectory(prefix="boswas-pki-") as tmp:
            csr, ext = Path(tmp) / "server.csr", Path(tmp) / "server.ext"
            old = os.umask(0o077)
            try:
                proc = _openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                                "-keyout", str(self.server_key), "-out", str(csr), "-subj",
                                "/O=Boswas Group/CN=Boswas Control Plane")
            finally:
                os.umask(old)
            if proc.returncode != 0:
                raise PkiError("creating the server key failed")
            ext.write_text(f"basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                           f"extendedKeyUsage=serverAuth\nsubjectAltName={sans}\n")
            proc = _openssl("x509", "-req", "-in", str(csr), "-CA", str(self.server_ca_crt), "-CAkey",
                            str(self.server_ca_key), "-set_serial", str(_serial()), "-days", str(days),
                            "-extfile", str(ext), "-out", str(self.server_crt))
            if proc.returncode != 0:
                raise PkiError("signing the server certificate failed")
        os.chmod(self.server_key, 0o600)
        os.chmod(self.server_crt, 0o644)

    # --- devices ---------------------------------------------------------------------------
    def check_csr(self, csr_pem: str) -> None:
        if len(csr_pem) > MAX_CSR or not _CSR_RE.fullmatch(csr_pem.replace("\r\n", "\n")):
            raise PkiError("not a PEM certificate request")
        verify = _openssl("req", "-noout", "-verify", input_bytes=csr_pem.encode("ascii"))
        if verify.returncode != 0:
            raise PkiError("the certificate request signature is not valid")
        pub = _openssl("req", "-noout", "-pubkey", input_bytes=csr_pem.encode("ascii"))
        text = _openssl("pkey", "-pubin", "-noout", "-text_pub", input_bytes=pub.stdout).stdout.decode(errors="replace")
        m = re.search(r"Public-Key: \((\d+) bit\)", text)
        bits = int(m.group(1)) if m else 0
        ec_ok = ("prime256v1" in text or "secp384r1" in text) and bits in (256, 384)
        rsa_ok = "Modulus" in text and bits >= 2048
        if not (ec_ok or rsa_ok):
            raise PkiError("the device key must be EC P-256/P-384 or RSA with at least 2048 bits")

    def issue_device_certificate(self, csr_pem: str, device_id: str, days: int = 365) -> IssuedCertificate:
        self.check_csr(csr_pem)
        with tempfile.TemporaryDirectory(prefix="boswas-pki-") as tmp:
            csr, ext, out = Path(tmp) / "device.csr", Path(tmp) / "device.ext", Path(tmp) / "device.crt"
            csr.write_text(csr_pem)
            ext.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                           "extendedKeyUsage=clientAuth\n")
            proc = _openssl("x509", "-req", "-in", str(csr), "-CA", str(self.device_ca_crt), "-CAkey",
                            str(self.device_ca_key), "-set_serial", str(_serial()), "-days", str(days),
                            "-subj", f"/O=Boswas OS device/CN={device_id}", "-extfile", str(ext), "-out", str(out))
            if proc.returncode != 0:
                raise PkiError("signing the device certificate failed")
            pem = out.read_text()
            end = _openssl("x509", "-noout", "-enddate", "-in", str(out)).stdout.decode().strip()
        expires = datetime.strptime(end.split("=", 1)[1], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return IssuedCertificate(pem, fingerprint_pem(pem), expires.strftime("%Y-%m-%dT%H:%M:%SZ"))

    # --- runtime ------------------------------------------------------------------------------
    def server_context(self) -> ssl.SSLContext:
        """Device port: devices may present a certificate from the device CA (verified if presented)."""
        ctx = self._tls_server()
        ctx.load_verify_locations(cafile=str(self.device_ca_crt))
        ctx.verify_mode = ssl.CERT_OPTIONAL
        return ctx

    def operator_context(self) -> ssl.SSLContext:
        """Operator port: never asks for a client certificate, so browsers show no certificate prompt."""
        ctx = self._tls_server()
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _tls_server(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(self.server_crt), str(self.server_key))
        return ctx

    def policy_public_key(self) -> str:
        return self.policy_pub.read_text()


def _serial() -> int:
    return int.from_bytes(secrets.token_bytes(16), "big") >> 1


def _is_ip(value: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False
