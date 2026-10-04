"""End to end: the real device agent and the real Control Plane over mutual TLS.

The Control Plane serves its device and operator ports on 127.0.0.1; the agent (with its
real HTTPS client and file credential store) enrolls, sends heartbeats and
inventory, verifies and applies the signed policy, receives typed commands
created by an operator through the API, downloads an installer over mutual
TLS, reports results, survives a Control Plane outage and is cut off when
retired. A fake user session stands in for boswas-winapp.

Run: python3 -m unittest discover -s tests   (needs the openssl command)
"""

import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
sys.path.append(str(HERE.parents[2] / "packages/boswas-device-agent/tests"))

from cp_testlib import ADMIN, manifest, pe_bytes  # noqa: E402
from agent_testlib import POSTURE, RUNTIME_OK, FakeInventory, FakeSessions, fake_run  # noqa: E402

from boswas_agent import daemon  # noqa: E402
from boswas_agent.models import ConnectionState  # noqa: E402
from boswas_agent.paths import AgentPaths  # noqa: E402
from boswas_cp.api import DEVICE, OPERATOR, Api  # noqa: E402
from boswas_cp.auth import LocalTokenAuthenticator  # noqa: E402
from boswas_cp.pki import Pki  # noqa: E402
from boswas_cp.server import serve  # noqa: E402
from boswas_cp.service import ControlPlane  # noqa: E402
from boswas_cp.store import Store  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        os.chmod(self.tmp, 0o755)
        # Control Plane
        data = self.tmp / "cp"
        self.pki = Pki(data)
        self.pki.initialize(["127.0.0.1", "localhost"])
        self.store = Store(data / "control-plane.db")
        self.addCleanup(self.store.close)
        self.port, self.operator_port = free_port(), free_port()
        self.url = f"https://127.0.0.1:{self.port}"
        self.cp = ControlPlane(self.store, self.pki, public_url=self.url, artifacts_dir=data / "artifacts")
        self.cp.ensure_default_policy()
        dashboard = self.tmp / "dashboard"
        dashboard.mkdir()
        (dashboard / "index.html").write_text("<!doctype html><title>Boswas</title>")
        self.api = Api(self.cp, LocalTokenAuthenticator(self.store), dashboard=dashboard)
        self.start_server()
        self.admin = self.cp.add_operator("test", "admin", "admin")["token"]
        self.viewer = self.cp.add_operator("test", "viewer", "viewer")["token"]
        self.operator = self.cp.add_operator("test", "ops", "operator")["token"]
        # Device
        self.paths = AgentPaths(self.tmp / "device")
        (self.paths.root / "etc/boswas").mkdir(parents=True)
        (self.paths.root / "etc/boswas/cp-ca.pem").write_bytes(self.pki.server_ca_crt.read_bytes())
        self.paths.device_conf.write_text(f'CONTROL_PLANE_URL="{self.url}"\nCONTROL_PLANE_CA="/etc/boswas/cp-ca.pem"\n'
                                          'TELEMETRY_POLICY="security"\nINVENTORY_POLICY="standard"\n')
        env = mock.patch.dict(os.environ, {"BOSWAS_SYSROOT": str(self.paths.root)})
        env.start()
        self.addCleanup(env.stop)
        self.sessions = FakeSessions()
        self.agent = daemon.Agent(self.paths, run=fake_run({("/usr/bin/boswas", "--json", "status"): (0, POSTURE),
                                                            ("/usr/bin/boswas-winapp", "--json", "runtime"): (0, RUNTIME_OK)}),
                                  inventory=FakeInventory(), sessions=self.sessions, is_root=False)
        self.agent.initialize()

    def start_server(self):
        self.servers = [serve(self.api, self.pki.server_context(), "127.0.0.1", self.port, DEVICE),
                        serve(self.api, self.pki.operator_context(), "127.0.0.1", self.operator_port, OPERATOR)]
        self.addCleanup(self.stop_server)

    def stop_server(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.servers = []

    # --- an HTTPS client: operator port unless device=True ------------------------------------------
    def http(self, method, path, body=None, token=None, raw=None, ctype=None, extra=None, cert=None, device=False):
        ctx = ssl.create_default_context(cafile=str(self.pki.server_ca_crt))
        if cert:
            ctx.load_cert_chain(*cert)
        port = self.port if device else self.operator_port
        conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=30)
        headers = dict(extra or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if ctype:
            headers["Content-Type"] = ctype
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            return (resp.status, json.loads(payload) if payload and resp.getheader("Content-Type", "").startswith(
                "application/json") else payload, dict(resp.getheaders()))
        finally:
            conn.close()

    def enroll(self):
        status, token, _ = self.http("POST", "/api/v1/enrollment-tokens", {"ttl_hours": 1}, token=self.admin)
        self.assertEqual(status, 201)
        result = self.agent.enroll(token["token"])
        self.assertTrue(result["enrolled"])
        return result

    def sync(self):
        self.agent.sync()
        self.agent.process_pending()
        self.agent.sync()

    # --- tests -----------------------------------------------------------------------------------
    def test_enrollment_heartbeat_inventory_policy_and_commands(self):
        self.enroll()
        device_id = self.agent.device_id
        self.agent.refresh_inventory(force=True)
        self.sync()
        self.assertEqual(self.agent.connection_state(), ConnectionState.CONNECTED)
        self.assertEqual(self.agent.policy.version(), "default-1")            # verified and applied
        self.assertTrue(self.paths.managed_policy.is_file())
        status, device, _ = self.http("GET", f"/api/v1/devices/{device_id}", token=self.viewer)
        self.assertEqual((status, device["connection"], device["device_state"], device["policy_current"]),
                         (200, "online", "READY", True))
        status, inv, _ = self.http("GET", f"/api/v1/devices/{device_id}/inventory", token=self.viewer)
        self.assertEqual(inv["document"]["os"]["version_id"], "1.0~alpha3")
        status, st, _ = self.http("GET", f"/api/v1/devices/{device_id}/status", token=self.viewer)
        self.assertEqual(st["compliance"]["document"]["summary"]["state"], "COMPLIANT")

        # A typed command from an operator, executed in the user's session.
        self.sessions.installed["com.example.app"] = {"id": "com.example.app", "version": "2.0",
                                                      "state": "installed", "sha256": "a" * 64}
        status, cmd, _ = self.http("POST", f"/api/v1/devices/{device_id}/commands",
                                   {"type": "LAUNCH_APPLICATION", "application_id": "com.example.app"},
                                   token=self.operator)
        self.assertEqual((status, cmd["status"]), (201, "QUEUED"))
        self.sync()
        status, done, _ = self.http("GET", f"/api/v1/devices/{device_id}/commands/{cmd['command_id']}",
                                    token=self.viewer)
        self.assertEqual((done["status"], done["result"]["started"]), ("SUCCEEDED", True))
        self.assertIn(("apps.launch", {"id": "com.example.app"}), self.sessions.calls)
        status, events, _ = self.http("GET", f"/api/v1/devices/{device_id}/events", token=self.viewer)
        types = {e["type"] for e in events["events"]}
        self.assertTrue({"DEVICE_REGISTERED", "DEVICE_ONLINE", "COMMAND_CREATED", "COMMAND_COMPLETED",
                         "APPLICATION_LAUNCHED"} <= types, types)
        self.assertTrue(self.http("GET", "/api/v1/events/verify", token=self.admin)[1]["valid"])

    def test_remote_install_downloads_the_installer_over_mutual_tls(self):
        self.enroll()
        installer = pe_bytes(tag=b"Nullsoft Install System example")
        status, art, _ = self.http("POST", "/api/v1/artifacts", raw=installer, ctype="application/octet-stream",
                                   token=self.admin, extra={"X-Boswas-File-Name": "example-setup.exe"})
        self.assertEqual((status, art["machine"]), (201, "x86_64"))
        status, app, _ = self.http("POST", "/api/v1/applications", {"manifest": manifest(sha=art["sha256"])},
                                   token=self.admin)
        self.assertEqual((status, app["installable"]), (201, True))
        device_id = self.agent.device_id
        status, cmd, _ = self.http("POST", f"/api/v1/devices/{device_id}/commands",
                                   {"type": "INSTALL_APPLICATION", "application_id": "com.example.app"},
                                   token=self.operator)
        self.assertEqual(status, 201)
        self.sync()
        status, done, _ = self.http("GET", f"/api/v1/devices/{device_id}/commands/{cmd['command_id']}",
                                    token=self.viewer)
        self.assertEqual(done["status"], "SUCCEEDED", done)
        self.assertEqual(self.sessions.installed["com.example.app"]["sha256"], hashlib.sha256(installer).hexdigest())
        self.assertTrue((self.paths.managed_manifests / "com.example.app.json").is_file())
        # The artifact endpoint needs a device certificate, and is not on the operator port at all.
        artifact = f"/api/v1/artifacts/{art['sha256']}"
        self.assertEqual(self.http("GET", artifact, token=self.admin, device=True)[0], 401)
        self.assertEqual(self.http("GET", artifact, token=self.admin)[1]["error"]["code"], "WRONG_PORT")

    def test_a_rogue_command_in_the_database_is_rejected_by_the_device(self):
        self.enroll()
        device_id, command_id = self.agent.device_id, str(uuid.uuid4())
        with self.store.transaction() as conn:          # simulates a tampered Control Plane database
            conn.execute("INSERT INTO commands (command_id, device_id, type, payload, status, created_at, expires_at, "
                         "created_by, fingerprint, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (command_id, device_id, "EXECUTE_SHELL_COMMAND", json.dumps({"command": "id"}), "QUEUED",
                          self.cp.stamp(), "2099-01-01T00:00:00Z", "operator:x", "x", self.cp.stamp()))
        self.sync()
        row = self.store.one("SELECT status, error_code FROM commands WHERE command_id = ?", (command_id,))
        self.assertEqual((row["status"], row["error_code"]), ("FAILED", "UNSUPPORTED_COMMAND"))
        self.assertEqual(self.sessions.calls, [])

    def test_control_plane_outage_and_recovery(self):
        self.enroll()
        self.sync()
        self.stop_server()
        for _ in range(3):
            self.agent.sync()
        self.assertEqual(self.agent.connection_state(), ConnectionState.OFFLINE)
        self.agent.evaluate()
        self.assertEqual(self.agent.machine.state.value, "OFFLINE")
        self.assertEqual(self.agent.operations()["agent.status"].handler(None)["connection"], "OFFLINE")
        self.agent.refresh_inventory(force=True)                          # queued while offline
        self.assertGreater(len(self.agent.outbox), 0)
        self.start_server()
        self.agent.sync()
        self.assertEqual(self.agent.connection_state(), ConnectionState.CONNECTED)
        self.assertEqual(len(self.agent.outbox), 0)

    def test_retired_devices_are_cut_off(self):
        self.enroll()
        self.sync()
        status, _, _ = self.http("POST", f"/api/v1/devices/{self.agent.device_id}/retire", token=self.admin)
        self.assertEqual(status, 200)
        self.agent.sync()
        self.assertEqual(self.agent.connection_state(), ConnectionState.REVOKED)

    def test_authentication_and_authorisation(self):
        self.enroll()
        device_id = self.agent.device_id
        creds = self.agent.credentials
        cert = (str(creds.cert), str(creds.key))
        self.assertEqual(self.http("GET", "/api/v1/health")[0], 200)
        self.assertEqual(self.http("GET", "/api/v1/devices")[0], 401)
        self.assertEqual(self.http("GET", "/api/v1/devices", cert=cert)[0], 401)          # a device is no operator
        self.assertEqual(self.http("GET", "/api/v1/devices", token="bcp_" + "0" * 12 + "_" + "A" * 43)[0], 401)
        self.assertEqual(self.http("POST", f"/api/v1/devices/{device_id}/commands", {"type": "REFRESH_INVENTORY"},
                                   token=self.viewer)[0], 403)
        self.assertEqual(self.http("POST", "/api/v1/policies", {"name": "x"}, token=self.operator)[0], 403)
        self.assertEqual(self.http("POST", "/api/v1/enrollment-tokens", {}, token=self.operator)[0], 403)
        hb = {"schema": "boswas-heartbeat/2", "device_id": device_id, "agent_version": "x", "state": "READY",
              "policy_version": None, "inventory_revision": None, "pending_results": 0, "sent_at": "2026-10-04T00:00:00Z"}
        heartbeat = f"/api/v1/devices/{device_id}/heartbeat"
        self.assertEqual(self.http("POST", heartbeat, hb, device=True)[0], 401)                          # no cert
        self.assertEqual(self.http("POST", heartbeat, hb, cert=cert, device=True)[0], 200)
        other = str(uuid.uuid4())
        self.assertEqual(self.http("POST", f"/api/v1/devices/{other}/heartbeat", {**hb, "device_id": other},
                                   cert=cert, device=True)[0], 403)
        # A certificate from a CA the Control Plane does not trust never completes the handshake.
        rogue_ca = Pki(self.tmp / "rogue")
        rogue_ca.initialize(["127.0.0.1"])
        with self.assertRaises((ssl.SSLError, ConnectionError, OSError)):
            self.http("POST", heartbeat, hb, cert=(str(rogue_ca.server_crt), str(rogue_ca.server_key)), device=True)

    def test_device_and_operator_ports_serve_disjoint_apis(self):
        self.enroll()
        device_id = self.agent.device_id
        cert = (str(self.agent.credentials.cert), str(self.agent.credentials.key))
        hb = {"schema": "boswas-heartbeat/2", "device_id": device_id, "agent_version": "x", "state": "READY",
              "policy_version": None, "inventory_revision": None, "pending_results": 0, "sent_at": "2026-10-04T00:00:00Z"}
        # The operator port never asks for a client certificate (no browser certificate prompt) ...
        self.assertEqual(self.pki.operator_context().verify_mode, ssl.CERT_NONE)
        self.assertEqual(self.pki.server_context().verify_mode, ssl.CERT_OPTIONAL)
        # ... and serves no device routes, even to a client holding a device certificate.
        status, body, _ = self.http("POST", f"/api/v1/devices/{device_id}/heartbeat", hb, cert=cert)
        self.assertEqual((status, body["error"]["code"]), (404, "WRONG_PORT"))
        self.assertEqual(self.http("POST", "/api/v1/enroll", {"schema": "x"})[1]["error"]["code"], "WRONG_PORT")
        self.assertEqual(self.http("GET", "/api/v1/artifacts/" + "0" * 64, cert=cert)[1]["error"]["code"],
                         "WRONG_PORT")
        # The device port serves no operator routes and no dashboard.
        status, body, _ = self.http("GET", "/api/v1/devices", token=self.admin, device=True)
        self.assertEqual((status, body["error"]["code"]), (404, "WRONG_PORT"))
        self.assertEqual(self.http("GET", "/", device=True)[0], 404)
        self.assertEqual(self.http("GET", "/app.js", device=True)[0], 404)
        # Both answer health; unknown paths are plain 404s on both.
        self.assertEqual(self.http("GET", "/api/v1/health", device=True)[0], 200)
        self.assertEqual(self.http("GET", "/api/v1/health")[0], 200)
        self.assertEqual(self.http("GET", "/api/v1/nothing", device=True)[1]["error"]["code"], "NOT_FOUND")

    def test_http_hygiene(self):
        status, body, headers = self.http("GET", "/api/v1/nothing")
        self.assertEqual(status, 404)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.http("DELETE", "/api/v1/health")[0], 405)
        self.assertEqual(self.http("POST", "/api/v1/policies", raw=b"name=x", ctype="text/plain", token=self.admin)[0],
                         415)
        self.assertEqual(self.http("POST", "/api/v1/policies", raw=b"{" * (4 * 1024 * 1024 + 1),
                                   ctype="application/json", token=self.admin)[0], 413)
        self.assertEqual(self.http("POST", "/api/v1/policies", raw=b"{not json", ctype="application/json",
                                   token=self.admin)[0], 400)
        status, page, headers = self.http("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, body, _ = self.http("GET", "/api/v1/summary", token=self.viewer)
        self.assertEqual((status, body["policies"]["count"]), (200, 1))


if __name__ == "__main__":
    unittest.main()
