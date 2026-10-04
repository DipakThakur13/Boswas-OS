"""Test doubles for the device agent: a real openssl CA, an in-memory Control
Plane client, a fake inventory, fake user sessions and a fake command runner."""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "boswas-compat"))

from boswas_agent import policydoc  # noqa: E402
from boswas_agent.errors import ConnectivityError, SessionUnavailable  # noqa: E402
from boswas_agent.interfaces import ControlPlaneClient, EnrollmentResult, HeartbeatResponse  # noqa: E402


def openssl(*args: str, input_bytes: bytes | None = None) -> bytes:
    proc = subprocess.run(["openssl", *args], input=input_bytes, capture_output=True, check=True)
    return proc.stdout


class TestCA:
    """A throw-away certificate authority (EC P-256), like the Control Plane's device CA."""

    def __init__(self, directory: Path, name: str = "Boswas Test CA"):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.key, self.cert = self.dir / "ca.key", self.dir / "ca.crt"
        openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                "-keyout", str(self.key), "-out", str(self.cert), "-subj", f"/CN={name}", "-days", "2")

    def sign(self, csr_pem: str, *, subject: str | None = None, server_names: list[str] | None = None) -> str:
        csr = self.dir / f"{secrets.token_hex(4)}.csr"
        out = self.dir / f"{secrets.token_hex(4)}.crt"
        ext = self.dir / f"{secrets.token_hex(4)}.ext"
        csr.write_text(csr_pem)
        args = ["x509", "-req", "-in", str(csr), "-CA", str(self.cert), "-CAkey", str(self.key),
                "-set_serial", str(int.from_bytes(secrets.token_bytes(8), "big")), "-days", "1", "-out", str(out)]
        if server_names:
            sans = ",".join(f"IP:{n}" if n.replace(".", "").isdigit() else f"DNS:{n}" for n in server_names)
            ext.write_text(f"subjectAltName={sans}\nextendedKeyUsage=serverAuth\n")
            args += ["-extfile", str(ext)]
        if subject:
            args += ["-subj", subject]
        openssl(*args)
        return out.read_text()

    def server_certificate(self, names: list[str]) -> tuple[Path, Path]:
        key, csr_path = self.dir / "server.key", self.dir / "server.csr"
        openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", str(key),
                "-out", str(csr_path), "-subj", "/CN=Boswas Control Plane")
        cert = self.dir / "server.crt"
        cert.write_text(self.sign(csr_path.read_text(), server_names=names))
        return cert, key


class FakeControlPlane(ControlPlaneClient):
    def __init__(self, ca: TestCA, policy_key: Path, policy_pub: str):
        self.ca, self.policy_key, self.policy_pub = ca, policy_key, policy_pub
        self.heartbeats, self.sent, self.acks, self.enrollments = [], [], [], []
        self.commands: list[dict] = []
        self.policy_envelope: dict | None = None
        self.policy_version: str | None = None
        self.artifacts: dict[str, bytes] = {}
        self.fail = False
        self.reject = None
        self.fetches = 0

    def _check(self):
        if self.fail:
            raise ConnectivityError("Control Plane unreachable: ConnectionRefusedError")
        if self.reject is not None:
            raise self.reject

    def set_policy(self, doc: dict) -> None:
        self.policy_envelope = policydoc.make_envelope(doc, self.policy_key, self.policy_pub)
        self.policy_version = doc["version"]

    def enroll(self, request):
        self._check()
        self.enrollments.append(request)
        cert = self.ca.sign(request.csr_pem, subject=f"/O=Boswas OS device/CN={request.device_id}")
        return EnrollmentResult(device_id=request.device_id, certificate_pem=cert, control_plane_url="https://cp.test",
                                policy_version=None, profile=request.profile, policy_public_key_pem=self.policy_pub)

    def heartbeat(self, heartbeat):
        self._check()
        self.heartbeats.append(heartbeat)
        return HeartbeatResponse(accepted=True, policy_version_available=self.policy_version,
                                 commands_pending=len(self.commands), next_heartbeat_seconds=300)

    def send(self, kind, document):
        self._check()
        self.sent.append((kind, document))

    def fetch_policy(self):
        self._check()
        return self.policy_envelope

    def fetch_commands(self):
        self._check()
        self.fetches += 1
        out, self.commands = self.commands, []
        return out

    def acknowledge(self, command_id, status):
        self._check()
        self.acks.append((command_id, status))

    def download_artifact(self, sha256, destination, max_bytes):
        self._check()
        data = self.artifacts[sha256]
        Path(destination).write_bytes(data[:max_bytes + 1])

    def results(self) -> dict[str, dict]:
        return {d["command_id"]: d for k, d in self.sent if k == "result"}


class FakeInventory:
    def __init__(self):
        self.calls = 0
        self.apps = []

    def os_info(self):
        return {"name": "Boswas OS", "version": "v1 Alpha", "version_id": "1.0~alpha3", "build_id": "BOS-T",
                "debian_version": "13.7", "kernel": "6.12.0-test", "architecture": "amd64"}

    def hardware(self):
        return {"cpu_model": "Test CPU", "cpu_count": 4, "memory_gib": 8.0, "vendor": "QEMU", "model": "Standard PC",
                "firmware_version": "1.0", "boot_mode": "UEFI", "tpm_version": None}

    def collect(self, policy="standard"):
        self.calls += 1
        doc = {"policy": policy, "os": self.os_info(), "packages": [{"name": "boswas-compat", "version": "1.0~alpha3"}],
               "windows_applications": list(self.apps), "windows_inventory_available": True,
               "compatibility": {"available": True, "wine_version": "wine-10.0", "architectures": ["x86_64"],
                                 "bubblewrap": "0.11", "apparmor": "enforce", "policy_managed": False, "healthy": True}}
        if policy == "standard":
            doc["hardware"] = self.hardware()
            doc["storage"] = {"root_total_gib": 100.0, "root_free_gib": 50.0}
        return doc


class FakeChannel:
    def __init__(self, owner):
        self.owner = owner

    def dispatch(self, op, params, timeout):
        return self.owner.handle(op, params)


class FakeSessions:
    """Stands in for the user's session agent (boswas-winapp as the user)."""

    def __init__(self):
        self.available = True
        self.calls = []
        self.installed: dict[str, dict] = {}
        self.running: set[str] = set()

    def pick(self):
        if not self.available:
            raise SessionUnavailable("no user is logged in with a running Boswas session agent")
        return FakeChannel(self)

    def summary(self):
        return {"connected": 1 if self.available else 0}

    def register(self, request):
        raise AssertionError("not used")

    def handle(self, op, params):
        from boswas_agent.localapi import ApiError
        self.calls.append((op, dict(params)))
        app_id = params.get("id")
        if op == "apps.status":
            if app_id not in self.installed:
                raise ApiError("NOT_FOUND", f"{app_id} is not installed")
            app = self.installed[app_id]
            return {"application": {**app, "app_state": "RUNNING" if app_id in self.running else "INSTALLED"},
                    "installer": {"sha256": app["sha256"]}, "policy": {"allowed": True}}
        if op in ("apps.install", "apps.upgrade"):
            import hashlib
            sha = hashlib.sha256(Path(params["path"]).read_bytes()).hexdigest()
            self.installed[app_id] = {"id": app_id, "version": "2.0", "state": "installed", "sha256": sha}
            return {"application": {"id": app_id, "version": "2.0", "state": "installed", "app_state": "INSTALLED"}}
        if op == "apps.remove":
            self.installed.pop(app_id, None)
            return {"application": app_id, "removed": True}
        if op == "apps.launch":
            self.running.add(app_id)
            return {"application": app_id, "started": True}
        if op == "apps.stop":
            was = app_id in self.running
            self.running.discard(app_id)
            return {"application": app_id, "was_running": was, "stopped": was}
        if op == "apps.repair":
            return {"application": app_id, "healthy": True}
        raise ApiError("UNKNOWN_OPERATION", op)


def fake_run(responses: dict | None = None, calls: list | None = None):
    responses = responses or {}

    def run(argv, **kwargs):
        if calls is not None:
            calls.append(list(argv))
        for prefix, (code, out) in responses.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
        return subprocess.CompletedProcess(argv, 127, stdout="", stderr="not available")
    return run


def command_doc(device_id: str, ctype: str, payload: dict | None = None, *, expires: str | None = None,
                created: str | None = None, command_id: str | None = None) -> dict:
    """A command created an hour ago that expires in a day (unless given)."""
    from datetime import timedelta

    from boswas_agent.commands import format_time, utc_now
    now = utc_now()
    return {"schema": "boswas-device-command/2", "command_id": command_id or str(uuid.uuid4()),
            "device_id": device_id, "type": ctype, "payload": {} if payload is None else payload,
            "created_at": created or format_time(now - timedelta(hours=1)),
            "expires_at": expires or format_time(now + timedelta(days=1)), "created_by": "operator:1"}


POSTURE = json.dumps({"compliance": {"state": "COMPLIANT_WITH_WARNINGS", "pass": 6, "warn": 2, "fail": 0,
                                     "unknown": 0},
                      "checks": [{"id": "firewall", "status": "PASS", "scored": True},
                                 {"id": "apparmor", "status": "WARN", "scored": True},
                                 {"id": "home-listing", "status": "PASS", "scored": True}]})
RUNTIME_OK = json.dumps({"healthy": True, "apparmor": {"mode": "enforce"}, "architectures": ["x86_64"]})
