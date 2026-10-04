"""Shared fixtures for the Control Plane tests: a fresh Control Plane in a
temporary directory, device enrollment requests with real CSRs, PE files."""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
for path in (HERE.parents[1], REPO / "packages/boswas-device-agent", REPO / "packages/boswas-compat"):
    sys.path.insert(0, str(path))

from boswas_agent import models  # noqa: E402
from boswas_cp.auth import Principal  # noqa: E402
from boswas_cp.pki import Pki  # noqa: E402
from boswas_cp.service import ControlPlane  # noqa: E402
from boswas_cp.store import Store  # noqa: E402

ADMIN = Principal("operator:admin", "admin")
OPERATOR = Principal("operator:ops", "operator")


def pe_bytes(machine: int = 0x8664, tag: bytes = b"") -> bytes:
    data = bytearray(0x200)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, machine)
    struct.pack_into("<H", data, 0x80 + 24, 0x20B if machine == 0x8664 else 0x10B)
    struct.pack_into("<H", data, 0x80 + 24 + 68, 3)
    return bytes(data) + tag


def manifest(app_id="com.example.app", version="2.0", sha="a" * 64, **overrides) -> dict:
    doc = {"id": app_id, "name": "Example", "version": version, "publisher": "Example Corp",
           "runtime": {"type": "wine", "wineVersion": "10"}, "architecture": "x86_64",
           "launch": "C:\\Program Files\\Example\\example.exe", "status": "approved",
           "installer": {"type": "exe", "sha256": sha, "silentArgs": ["/S"]},
           "sandbox": {"network": False, "display": True}}
    doc.update(overrides)
    return doc


def csr(device_id: str, directory: Path) -> str:
    key = directory / f"{device_id}.key"
    out = subprocess.run(["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                          "-keyout", str(key), "-subj", f"/CN=whatever-the-device-says"],
                         capture_output=True, check=True, text=True)
    return out.stdout


def enrollment_request(token: str, directory: Path, device_id: str | None = None, **overrides) -> dict:
    device_id = device_id or str(uuid.uuid4())
    req = models.EnrollmentRequest(
        device_id=device_id, csr_pem=csr(device_id, directory),
        os=models.OsInfo("Boswas OS", "v1 Alpha", "1.0~alpha3", "BOS-T", "13.7", "6.12.0"),
        hardware=models.HardwareFacts("QEMU", "Standard PC", "1.0", "Test CPU", 8.0, None, "UEFI"),
        profile="engineering", enrollment_token=token, agent_version="1.0~alpha3", ephemeral=False,
        device_name="Test Laptop").to_dict()
    req.update(overrides)
    return req


class ControlPlaneCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.clock = [1_790_000_000.0]
        self.pki = Pki(self.tmp / "data")
        self.pki.initialize(["127.0.0.1", "localhost"])
        self.store = Store(self.tmp / "data/control-plane.db")
        self.addCleanup(self.store.close)
        self.cp = ControlPlane(self.store, self.pki, public_url="https://127.0.0.1:8443",
                               artifacts_dir=self.tmp / "data/artifacts", clock=lambda: self.clock[0])
        self.cp.ensure_default_policy()

    def token(self, **kw) -> str:
        return self.cp.create_enrollment_token(ADMIN, **kw)["token"]

    def enroll(self, **kw) -> tuple[dict, dict]:
        req = enrollment_request(self.token(), self.tmp, **kw)
        return req, self.cp.enroll(req)

    def device(self, device_id: str) -> dict:
        return self.store.one("SELECT * FROM devices WHERE device_id = ?", (device_id,))

    def heartbeat(self, device_id: str, **overrides) -> dict:
        doc = models.Heartbeat(device_id=device_id, agent_version="1.0~alpha3", state="READY",
                               policy_version="default-1", inventory_revision=1,
                               sent_at="2026-10-04T12:00:00Z").to_dict()
        doc.update(overrides)
        return doc

    def catalog_app(self, app_id="com.example.app", version="2.0", machine=0x8664, **manifest_overrides) -> dict:
        exe = self.tmp / f"{app_id}-{version}.exe"
        exe.write_bytes(pe_bytes(machine, f"{app_id}{version}".encode()))
        art = self.cp.import_artifact_file(ADMIN, exe)
        return self.cp.upsert_application(ADMIN, {"manifest": manifest(app_id, version, art["sha256"],
                                                                         **manifest_overrides)})

    def events(self, event_type: str) -> list[dict]:
        return self.cp.events(event_type=event_type, limit=1000)


def inventory_doc(device_id: str, revision: int = 1, apps=None) -> dict:
    return {"schema": "boswas-inventory/1", "device_id": device_id, "revision": revision,
            "collected_at": "2026-10-04T12:00:00Z", "policy": "minimal",
            "os": {"name": "Boswas OS", "version": "v1 Alpha", "version_id": "1.0~alpha3", "build_id": "BOS-T",
                   "debian_version": "13.7", "kernel": "6.12.0", "architecture": "amd64"},
            "packages": [{"name": "boswas-compat", "version": "1.0~alpha3"}],
            "windows_applications": apps if apps is not None else [
                {"id": "com.example.app", "version": "1.0", "status": "approved", "architecture": "x86_64",
                 "compatibility": "SUPPORTED", "app_state": "RUNNING", "installations": 1, "running": 1}],
            "windows_inventory_available": True,
            "compatibility": {"available": True, "wine_version": "wine-10.0", "architectures": ["x86_64"],
                              "bubblewrap": "0.11", "apparmor": "enforce", "policy_managed": True, "healthy": True}}


def dumps(doc) -> bytes:
    return json.dumps(doc).encode()
