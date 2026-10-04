"""Run a throw-away Boswas Control Plane from the repository sources, and talk to it.

Shared by tests/device/device_scenario.py (inside the image) and
tests/boot/qemu_boot_test.py (builder container, devices in a VM). Standard
library only.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import ssl
import subprocess
import sys
import time
from pathlib import Path

CLI = ("import sys; sys.path[:0] = sys.argv[1].split(':')\n"
       "from boswas_cp.cli import main\nsys.exit(main(sys.argv[2:]))")


class ControlPlane:
    def __init__(self, *, python_path: list[str], data_dir: Path, port: int, public_url: str, listen: str = "127.0.0.1",
                 operator_port: int | None = None, dashboard: Path | None = None, log: Path | None = None):
        """port: the device port (listen address); operator_port (default port + 1): the operator
        API, on 127.0.0.1 only, which is where this harness talks to it from."""
        self.python_path = ":".join(str(p) for p in python_path)
        self.data = Path(data_dir)
        self.port, self.public_url = port, public_url
        self.operator_port = operator_port or port + 1
        self.conf = self.data.parent / f"{self.data.name}.conf"
        self.conf.parent.mkdir(parents=True, exist_ok=True)
        lines = [f'LISTEN_ADDRESS="{listen}"', f'LISTEN_PORT="{port}"', f'PUBLIC_URL="{public_url}"',
                 'OPERATOR_LISTEN_ADDRESS="127.0.0.1"', f'OPERATOR_LISTEN_PORT="{self.operator_port}"',
                 f'DATA_DIR="{self.data}"']
        if dashboard:
            lines.append(f'DASHBOARD_DIR="{dashboard}"')
        self.conf.write_text("\n".join(lines) + "\n")
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.admin: str | None = None

    def cli(self, *args: str, timeout: float = 300) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", "-c", CLI, self.python_path, "--config", str(self.conf), *args],
                              capture_output=True, text=True, timeout=timeout,
                              env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"})

    def init(self, server_names: list[str]) -> str:
        args = ["--json", "init", "--public-url", self.public_url]
        for name in server_names:
            args += ["--server-name", name]
        proc = self.cli(*args)
        if proc.returncode != 0:
            raise RuntimeError(f"boswas-cp init failed: {proc.stdout}{proc.stderr}"[-1000:])
        self.admin = json.loads(proc.stdout)["admin"]["token"]
        return self.admin

    @property
    def ca_pem(self) -> str:
        return (self.data / "ca/server-ca.crt").read_text()

    def start(self, timeout: float = 60) -> None:
        out = open(self.log, "a") if self.log else subprocess.DEVNULL
        self.proc = subprocess.Popen([sys.executable, "-B", "-c", CLI, self.python_path, "--config", str(self.conf),
                                      "serve"], stdout=out, stderr=subprocess.STDOUT,
                                     env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if all(self.request("GET", "/api/v1/health", token=None, port=port)[0] == 200
                       for port in (self.operator_port, self.port)):
                    return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError("the Control Plane did not start")

    def stop(self) -> int | None:
        if self.proc is None or self.proc.poll() is not None:
            return self.proc.returncode if self.proc else None
        self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return None
        finally:
            self.proc = None

    def enrollment_token(self, *extra: str) -> str:
        proc = self.cli("--json", "token", "create", *extra)
        return json.loads(proc.stdout)["token"]

    def request(self, method: str, path: str, body=None, *, raw: bytes | None = None, name: str | None = None,
                token: str | None = "admin", port: int | None = None) -> tuple[int, dict]:
        """An operator API request (operator port unless another port is given)."""
        ctx = ssl.create_default_context(cafile=str(self.data / "ca/server-ca.crt"))
        ctx.check_hostname = False          # the test talks to 127.0.0.1 whatever names the certificate has
        conn = http.client.HTTPSConnection("127.0.0.1", port or self.operator_port, context=ctx, timeout=60)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {self.admin if token == 'admin' else token}"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if raw is not None:
            headers["Content-Type"] = "application/octet-stream"
            headers["X-Boswas-File-Name"] = name or "installer.exe"
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, json.loads(payload) if payload else {}
        finally:
            conn.close()

    def command(self, device_id: str, body: dict) -> tuple[int, dict]:
        return self.request("POST", f"/api/v1/devices/{device_id}/commands", body)

    def command_status(self, device_id: str, command_id: str) -> dict:
        return self.request("GET", f"/api/v1/devices/{device_id}/commands/{command_id}")[1]
