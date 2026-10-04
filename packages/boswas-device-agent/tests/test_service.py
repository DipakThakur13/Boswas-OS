"""Device agent service: the agent daemon with an in-memory Control Plane, the
command executor, the local management API (real Unix sockets and kernel
peer credentials), session agents, the boswas-winapp wrapper and the CLI.

Run: python3 -m unittest discover -s tests
Tests that switch to another user ID need root (they are skipped otherwise).
"""

import contextlib
import hashlib
import io
import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))

from agent_testlib import (POSTURE, RUNTIME_OK, FakeControlPlane, FakeInventory, FakeSessions,  # noqa: E402
                           TestCA, command_doc, fake_run)

from boswas_compat import UNSUPPORTED_32BIT_MESSAGE  # noqa: E402

from boswas_agent import cli, commands, daemon, localapi, policydoc, privacy, session_agent, sessions  # noqa: E402
from boswas_agent import winapp as winapp_mod  # noqa: E402
from boswas_agent.errors import BackendError, ControlPlaneRejected, SessionUnavailable  # noqa: E402
from boswas_agent.models import ConnectionState, DeviceState  # noqa: E402
from boswas_agent.paths import AgentPaths  # noqa: E402

TOKEN = "enroll-TOKEN-0123456789abcdef-SECRET"
ROOT = os.geteuid() == 0
OTHER_UID = 1500


def manifest(**overrides):
    doc = {"id": "com.example.app", "name": "Example", "version": "2.0", "runtime": {"type": "wine", "wineVersion": "10"},
           "architecture": "x86_64", "launch": "C:\\Program Files\\Example\\example.exe", "status": "approved"}
    doc.update(overrides)
    return doc


class AgentCase(unittest.TestCase):
    managed = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        os.chmod(self.root, 0o755)
        self.paths = AgentPaths(self.root / "sys")
        (self.paths.root / "etc/boswas").mkdir(parents=True)
        conf = 'HEARTBEAT_INTERVAL="300"\nINVENTORY_POLICY="standard"\nTELEMETRY_POLICY="security"\n'
        if self.managed:
            conf += 'CONTROL_PLANE_URL="https://cp.test:8443"\nCONTROL_PLANE_CA="/etc/boswas/cp-ca.pem"\n'
        self.paths.device_conf.write_text(conf)
        env = mock.patch.dict(os.environ, {"BOSWAS_SYSROOT": str(self.paths.root)})
        env.start()
        self.addCleanup(env.stop)
        self.ca = TestCA(self.root / "ca")
        self.policy_key = self.root / "policy.key"
        self.policy_pub = policydoc.generate_signing_key(self.policy_key)
        self.cp = FakeControlPlane(self.ca, self.policy_key, self.policy_pub)
        self.sessions = FakeSessions()
        self.inventory = FakeInventory()
        self.run_calls = []
        self.run = fake_run({("/usr/bin/boswas", "--json", "status"): (0, POSTURE),
                             ("/usr/bin/boswas-winapp", "--json", "runtime"): (0, RUNTIME_OK),
                             ("dpkg-query",): (0, "1.0~alpha3"),
                             ("systemd-escape",): (0, "boswas-agent-update@1.0\\x7ealpha4.service\n"),
                             ("systemctl", "start"): (0, "")}, self.run_calls)
        self.clock = [1000.0]
        self.agent = self.make_agent()

    def make_agent(self):
        agent = daemon.Agent(self.paths, run=self.run, client_factory=lambda a: self.cp, inventory=self.inventory,
                             sessions=self.sessions, clock=lambda: self.clock[0], is_root=False)
        agent.initialize()
        return agent

    def enroll(self):
        result = self.agent.enroll(TOKEN)
        self.assertTrue(result["enrolled"])
        return result

    def files_text(self) -> str:
        texts = []
        for path in self.paths.root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                texts.append(path.read_bytes().decode("utf-8", errors="replace"))
        return "\n".join(texts)


# --- daemon ---------------------------------------------------------------------------------

class StandaloneTests(AgentCase):
    managed = False

    def test_standalone_device_works_without_any_control_plane(self):
        self.agent.tick()
        status = self.agent.status()
        self.assertEqual((status["connection"], status["state"]), ("STANDALONE", "READY"))
        self.assertTrue(status["device_id"])
        self.assertEqual(type(self.agent.client()).__name__, "FakeControlPlane")   # injected; not used
        self.assertEqual(self.cp.heartbeats, [])
        doc = json.loads(self.paths.status.read_text())
        self.assertEqual(doc["device_id"], status["device_id"])
        self.assertEqual(stat.S_IMODE(self.paths.status.stat().st_mode), 0o644)
        self.assertTrue(json.loads(self.paths.inventory.read_text())["os"])
        self.assertEqual(len(self.agent.outbox), 0)                    # nothing queued for anybody
        with self.assertRaises(localapi.ApiError):
            self.agent.enroll(TOKEN)


class ManagedAgentTests(AgentCase):
    def test_enrollment_stores_credentials_and_never_the_token(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        logging.getLogger("boswas-device-agent").addHandler(handler)
        self.addCleanup(logging.getLogger("boswas-device-agent").removeHandler, handler)
        self.enroll()
        req = self.cp.enrollments[0]
        self.assertEqual(req.device_id, self.agent.device_id)
        self.assertNotIn(TOKEN, repr(req))
        creds = self.paths.credentials
        self.assertEqual(stat.S_IMODE(creds.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((creds / "device.key").stat().st_mode), 0o600)
        self.assertTrue(self.agent.enrolled)
        self.agent.sync()
        self.agent.process_pending()
        self.assertNotIn(TOKEN, self.files_text())
        self.assertNotIn(TOKEN, "\n".join(records))
        self.assertNotIn("PRIVATE KEY", self.paths.status.read_text())

    def test_sync_applies_policy_executes_commands_and_reports(self):
        self.enroll()
        doc = policydoc.default_policy(sequence=2, issued_at="2026-10-04T12:00:00Z")
        doc["compat"]["unlisted_apps"] = "allow"
        self.cp.set_policy(doc)
        self.sessions.installed["com.example.app"] = {"id": "com.example.app", "version": "2.0",
                                                      "state": "installed", "sha256": "a" * 64}
        refresh = command_doc(self.agent.device_id, "REFRESH_INVENTORY")
        launch = command_doc(self.agent.device_id, "LAUNCH_APPLICATION", {"application_id": "com.example.app"})
        self.cp.commands = [refresh, launch]
        self.agent.sync()
        hb = self.cp.heartbeats[-1]
        privacy.check(hb)
        self.assertEqual(set(hb), {"schema", "device_id", "agent_version", "state", "policy_version",
                                   "inventory_revision", "pending_results", "sent_at"})
        self.assertEqual(self.agent.policy.version(), "default-2")
        self.assertIn('UNLISTED_APPS="allow"', self.paths.managed_policy.read_text())
        self.assertEqual(self.agent.next_sync, self.clock[0])     # the new version is reported right away
        self.assertEqual({c for c, s in self.cp.acks if s == "ACKNOWLEDGED"},
                         {refresh["command_id"], launch["command_id"]})
        self.agent.process_pending()
        self.agent.sync()
        results = self.cp.results()
        self.assertEqual(results[refresh["command_id"]]["status"], "SUCCEEDED")
        self.assertEqual(results[launch["command_id"]]["result"]["started"], True)
        self.assertIn(("apps.launch", {"id": "com.example.app"}), self.sessions.calls)
        kinds = {k for k, _ in self.cp.sent}
        self.assertTrue({"inventory", "status", "compliance", "event", "result"} <= kinds, kinds)
        for kind, sent in self.cp.sent:
            privacy.check(sent)
        status_report = next(d for k, d in self.cp.sent if k == "status")
        self.assertNotIn("home-listing", status_report["security"])       # only documented check IDs
        self.assertEqual(self.agent.status()["connection"], "CONNECTED")

    def test_offline_control_plane_never_blocks_local_work(self):
        self.enroll()
        self.agent.sync()
        self.cp.fail = True
        delays = []
        for _ in range(6):
            before = self.clock[0]
            self.agent.sync()
            delays.append(self.agent.next_sync - before)
        self.assertTrue(all(0 < d <= 900 for d in delays), delays)
        self.assertEqual(self.agent.connection_state(), ConnectionState.OFFLINE)
        self.agent.evaluate()
        self.assertEqual(self.agent.machine.state, DeviceState.OFFLINE)
        self.assertIn("local applications keep working", " ".join(self.agent.machine.reasons))
        # Local work: inventory, status and the local API still answer; queued data is kept.
        self.assertGreaterEqual(self.agent.refresh_inventory(force=True), 1)
        self.assertEqual(self.agent.operations()["agent.status"].handler(None)["connection"], "OFFLINE")
        queued = len(self.agent.outbox)
        self.assertGreater(queued, 0)
        self.cp.fail = False
        self.agent.sync()
        self.assertEqual(self.agent.connection_state(), ConnectionState.CONNECTED)
        self.assertEqual(len(self.agent.outbox), 0)
        self.agent.evaluate()
        self.assertEqual(self.agent.machine.state, DeviceState.READY)

    def test_expired_unknown_and_duplicate_commands(self):
        self.enroll()
        self.sessions.installed["com.example.app"] = {"id": "com.example.app", "version": "2.0",
                                                      "state": "installed", "sha256": "a" * 64}
        expired = command_doc(self.agent.device_id, "REFRESH_INVENTORY", created="2026-01-01T00:00:00Z",
                              expires="2026-01-02T00:00:00Z")
        unknown = command_doc(self.agent.device_id, "EXECUTE_SHELL_COMMAND", {"command": "id"})
        launch = command_doc(self.agent.device_id, "LAUNCH_APPLICATION", {"application_id": "com.example.app"})
        self.cp.commands = [expired, unknown, launch]
        self.agent.sync()
        self.agent.process_pending()
        self.agent.sync()
        results = self.cp.results()
        self.assertEqual(results[expired["command_id"]]["status"], "EXPIRED")
        self.assertEqual(results[unknown["command_id"]]["error"]["code"], "UNSUPPORTED_COMMAND")
        self.assertEqual(results[launch["command_id"]]["status"], "SUCCEEDED")
        self.assertTrue(any(e["type"] == "SECURITY_EVENT" for e in self.agent.events.recent()))
        launches = [c for c in self.sessions.calls if c[0] == "apps.launch"]
        self.cp.sent.clear()
        self.cp.commands = [launch]                           # delivered again (e.g. lost acknowledgement)
        self.agent.sync()
        self.agent.process_pending()
        self.agent.sync()
        self.assertEqual([c for c in self.sessions.calls if c[0] == "apps.launch"], launches)   # not run twice
        self.assertEqual(self.cp.results()[launch["command_id"]]["status"], "SUCCEEDED")         # result resent

    def test_commands_wait_for_a_user_session_until_they_expire(self):
        self.enroll()
        self.sessions.available = False
        launch = command_doc(self.agent.device_id, "LAUNCH_APPLICATION", {"application_id": "com.example.app"})
        self.cp.commands = [launch]
        self.agent.sync()
        self.agent.process_pending()
        entry = self.agent.ledger.get(launch["command_id"])
        self.assertEqual(entry["status"], "ACKNOWLEDGED")
        self.assertIn("no user", entry["waiting"])
        self.sessions.available = True
        self.sessions.installed["com.example.app"] = {"id": "com.example.app", "version": "2.0",
                                                      "state": "installed", "sha256": "a" * 64}
        self.agent.process_pending()
        self.assertEqual(self.agent.ledger.get(launch["command_id"])["status"], "SUCCEEDED")

    def test_interrupted_command_is_reported_after_a_restart(self):
        self.enroll()
        cmd = command_doc(self.agent.device_id, "REPAIR_APPLICATION", {"application_id": "com.example.app"})
        self.agent.ledger.record(cmd["command_id"], {"status": "RUNNING", "type": "REPAIR_APPLICATION",
                                                     "document": cmd, "received_at": "2026-10-04T12:00:00Z"})
        restarted = self.make_agent()
        self.assertEqual(restarted.ledger.get(cmd["command_id"])["status"], "FAILED")
        restarted.sync()
        self.assertEqual(self.cp.results()[cmd["command_id"]]["error"]["code"], "INTERRUPTED")

    def test_revoked_device_stops_contacting_the_control_plane(self):
        self.enroll()
        self.cp.reject = ControlPlaneRejected("revoked", code="DEVICE_REVOKED", status=403)
        self.agent.sync()
        self.assertEqual(self.agent.connection_state(), ConnectionState.REVOKED)
        self.assertFalse(self.agent.should_sync())
        self.assertTrue(any("no longer accepts" in e["detail"] for e in self.agent.events.recent()))

    def test_maintenance_pauses_remote_commands(self):
        self.enroll()
        self.agent.operations()["maintenance.set"].handler(
            localapi.Request(localapi.Peer(1, 0, 0), "maintenance.set", {"enabled": True}, None))
        self.cp.commands = [command_doc(self.agent.device_id, "REFRESH_INVENTORY")]
        self.agent.sync()
        self.assertEqual(self.cp.fetches, 0)
        self.assertEqual(self.agent.machine.state, DeviceState.MAINTENANCE)

    def test_unenroll_restores_the_local_policy(self):
        self.enroll()
        self.cp.set_policy(policydoc.default_policy(sequence=1, issued_at="2026-10-04T12:00:00Z"))
        self.agent.sync()
        self.assertTrue(self.paths.managed_policy.exists())
        self.agent.unenroll()
        self.assertFalse(self.paths.managed_policy.exists())
        self.assertFalse(self.agent.credentials.has_credential())
        self.assertEqual(len(self.agent.outbox), 0)
        self.assertEqual(self.agent.connection_state(), ConnectionState.UNENROLLED)

    def test_invalid_remote_policy_keeps_the_current_one(self):
        self.enroll()
        self.cp.set_policy(policydoc.default_policy(sequence=3, issued_at="2026-10-04T12:00:00Z"))
        self.agent.sync()
        good = self.paths.managed_policy.read_text()
        other_pub = policydoc.generate_signing_key(self.root / "rogue.key")
        rogue = policydoc.default_policy(sequence=9, issued_at="2026-10-05T12:00:00Z")
        rogue["compat"]["unlisted_apps"] = "allow"
        self.cp.policy_envelope = policydoc.make_envelope(rogue, self.root / "rogue.key", other_pub)
        self.cp.policy_version = "default-9"
        self.agent.sync()
        self.assertEqual(self.paths.managed_policy.read_text(), good)
        self.assertTrue(any("policy rejected" in e["detail"] for e in self.agent.events.recent()))
        self.assertGreater(self.agent.next_sync, self.clock[0])  # a rejected policy causes no sync loop


# --- command executor -------------------------------------------------------------------------

class ExecutorTests(AgentCase):
    def setUp(self):
        super().setUp()
        self.enroll()
        self.installer = b"MZ fake installer bytes"
        self.sha = hashlib.sha256(self.installer).hexdigest()
        self.cp.artifacts[self.sha] = self.installer

    def install_cmd(self, **manifest_overrides):
        m = manifest(installer={"type": "exe", "sha256": self.sha}, **manifest_overrides)
        payload = {"application_id": m["id"], "version": m["version"],
                   "installer": {"sha256": self.sha, "size": len(self.installer), "file_name": "setup.exe"},
                   "manifest": m}
        return commands.parse_command(command_doc(self.agent.device_id, "INSTALL_APPLICATION", payload))

    def test_install_downloads_verifies_and_dispatches(self):
        outcome = self.agent.executor.execute(self.install_cmd())
        self.assertEqual(outcome.status.value, "SUCCEEDED", outcome.error_message)
        op, params = self.sessions.calls[-1]
        self.assertEqual(op, "apps.install")
        self.assertTrue(params["path"].startswith(str(self.paths.artifacts)))
        self.assertFalse(Path(params["path"]).exists())                        # artifact removed afterwards
        self.assertTrue((self.paths.managed_manifests / "com.example.app.json").is_file())
        again = self.agent.executor.execute(self.install_cmd())               # idempotent
        self.assertEqual((again.status.value, again.result.get("already")), ("SUCCEEDED", True))

    def test_32_bit_entries_are_refused_before_any_download(self):
        outcome = self.agent.executor.execute(self.install_cmd(architecture="x86"))
        self.assertEqual((outcome.status.value, outcome.error_code), ("FAILED", "UNSUPPORTED_ARCHITECTURE"))
        self.assertEqual(outcome.error_message, UNSUPPORTED_32BIT_MESSAGE)
        self.assertEqual(self.sessions.calls, [])
        self.assertFalse(self.paths.artifacts.exists() and any(self.paths.artifacts.iterdir()))

    def test_device_policy_refusal(self):
        (self.paths.root / "etc/boswas/compat").mkdir(parents=True)
        (self.paths.root / "etc/boswas/compat/policy.conf").write_text(
            'ALLOWED_STATUSES="approved"\nBLOCKED_APPLICATIONS="com.example.app"\n')
        outcome = self.agent.executor.execute(self.install_cmd())
        self.assertEqual(outcome.error_code, "POLICY_REFUSED")
        self.assertEqual(self.sessions.calls, [])

    def test_tampered_download_is_never_installed(self):
        self.cp.artifacts[self.sha] = b"MZ something else entirely"
        outcome = self.agent.executor.execute(self.install_cmd())
        self.assertEqual(outcome.error_code, "ARTIFACT_MISMATCH")
        self.assertNotIn("apps.install", [c[0] for c in self.sessions.calls])

    def test_remove_stop_repair_are_idempotent(self):
        dev = self.agent.device_id
        remove = commands.parse_command(command_doc(dev, "REMOVE_APPLICATION", {"application_id": "com.none.app"}))
        self.assertEqual(self.agent.executor.execute(remove).result, {"application_id": "com.none.app",
                                                                      "removed": False, "already": True})
        self.sessions.installed["com.example.app"] = {"id": "com.example.app", "version": "2.0",
                                                      "state": "installed", "sha256": self.sha}
        stop = commands.parse_command(command_doc(dev, "STOP_APPLICATION", {"application_id": "com.example.app"}))
        self.assertEqual(self.agent.executor.execute(stop).result["was_running"], False)
        repair = commands.parse_command(command_doc(dev, "REPAIR_APPLICATION", {"application_id": "com.example.app"}))
        self.assertEqual(self.agent.executor.execute(repair).result["healthy"], True)

    def test_commands_not_allowed_by_policy_and_agent_updates(self):
        doc = policydoc.default_policy(sequence=1, issued_at="2026-10-04T12:00:00Z")
        doc["agent"]["allowed_commands"] = ["REFRESH_INVENTORY", "UPDATE_AGENT"]
        self.cp.set_policy(doc)
        self.agent.sync()
        dev = self.agent.device_id
        launch = commands.parse_command(command_doc(dev, "LAUNCH_APPLICATION", {"application_id": "com.example.app"}))
        self.assertEqual(self.agent.executor.execute(launch).error_code, "COMMAND_REFUSED")
        update = commands.parse_command(command_doc(dev, "UPDATE_AGENT", {"version": "1.0~alpha4"}))
        self.assertEqual(self.agent.executor.execute(update).error_code, "UPDATE_NOT_PERMITTED")
        self.assertFalse(any(c[:2] == ["systemctl", "start"] for c in self.run_calls))

    def test_session_unavailable_is_raised_for_retry(self):
        self.sessions.available = False
        launch = commands.parse_command(command_doc(self.agent.device_id, "LAUNCH_APPLICATION",
                                                    {"application_id": "com.example.app"}))
        with self.assertRaises(SessionUnavailable):
            self.agent.executor.execute(launch)


# --- local API over real sockets ------------------------------------------------------------------

class LocalApiTests(AgentCase):
    def setUp(self):
        super().setUp()
        self.agent.api = localapi.ApiServer(self.root / "run/agent.sock", self.agent.operations(), mode=0o666)
        self.agent.api.start()
        self.addCleanup(self.agent.api.stop)
        self.client = localapi.ApiClient(self.root / "run/agent.sock", timeout=20)

    def test_operations_and_validation(self):
        self.assertEqual(self.client.call("agent.status")["device_id"], self.agent.device_id)
        self.assertEqual(self.client.call("device.identity")["device_id"], self.agent.device_id)
        for op, params, code in (("shell.run", {}, "UNKNOWN_OPERATION"),
                                 ("events.list", {"limit": "ten"}, "BAD_REQUEST"),
                                 ("events.list", {"limit": 5, "extra": 1}, "BAD_REQUEST"),
                                 ("maintenance.set", {}, "BAD_REQUEST"),
                                 ("enroll", {"token": "short"}, "BAD_REQUEST")):
            with self.assertRaises(localapi.ApiError) as cm:
                self.client.call(op, **params)
            self.assertEqual(cm.exception.code, code, op)

    def test_malformed_messages_do_not_break_the_server(self):
        import socket
        for raw in (b"not json\n", b"[1,2]\n", b'{"v": 9, "op": "agent.status"}\n', b"\xff\xfe\n"):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(self.root / "run/agent.sock"))
            s.sendall(raw)
            reply = json.loads(s.recv(65536).split(b"\n")[0])
            s.close()
            self.assertFalse(reply["ok"])
        self.assertTrue(self.client.call("agent.status"))

    @unittest.skipUnless(ROOT, "needs root to act as another user")
    def test_privileged_operations_are_refused_to_users(self):
        code = ("import sys; sys.path[:0] = sys.argv[2:]\n"
                "from boswas_agent.localapi import ApiClient, ApiError\n"
                "c = ApiClient(sys.argv[1])\n"
                "print(c.call('agent.status')['state'])\n"
                "for op, p in (('maintenance.set', {'enabled': True}), ('unenroll', {}), ('sync.now', {}),\n"
                "              ('enroll', {'token': 'x' * 20}), ('inventory.refresh', {})):\n"
                "    try:\n"
                "        c.call(op, **p); print(op, 'ALLOWED')\n"
                "    except ApiError as e:\n"
                "        print(op, e.code)\n")
        proc = subprocess.run([sys.executable, "-c", code, str(self.root / "run/agent.sock"),
                               str(HERE.parents[1]), str(HERE.parents[2] / "boswas-compat")],
                              user=OTHER_UID, group=OTHER_UID, capture_output=True, text=True, timeout=60)
        lines = proc.stdout.split("\n")
        self.assertIn(lines[0], [s.value for s in DeviceState], proc.stderr)
        for line in lines[1:]:
            if line:
                self.assertTrue(line.endswith("FORBIDDEN"), line)
        self.assertFalse(self.agent.local["maintenance"])


class SessionChannelTests(unittest.TestCase):
    def test_registration_dispatch_and_refusal_of_other_operations(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = sessions.SessionRegistry(active_uid=lambda: None)
        server = localapi.ApiServer(Path(tmp.name) / "agent.sock",
                                    {"session.register": localapi.Operation("session.register", registry.register,
                                                                            "session")},
                                    min_session_uid=0)
        server.start()
        self.addCleanup(server.stop)
        seen = []

        def execute(op, params):
            seen.append((op, params))
            return {"application": {"id": params["id"], "app_state": "INSTALLED"}}
        link = sessions.SessionLink(str(Path(tmp.name) / "agent.sock"), execute)
        link.start()
        self.addCleanup(link.stop)
        self.assertTrue(link.connected.wait(10))
        for _ in range(50):
            if registry.summary()["connected"]:
                break
            time.sleep(0.05)
        result = registry.pick().dispatch("apps.status", {"id": "com.example.app"}, 10)
        self.assertEqual(result["application"]["app_state"], "INSTALLED")
        with self.assertRaises(localapi.ApiError):
            registry.pick().dispatch("system.shell", {"cmd": "id"}, 10)
        self.assertEqual(seen, [("apps.status", {"id": "com.example.app"})])
        reply = link._answer(1, {"op": "apps.shell", "params": {}})            # session side refuses too
        self.assertEqual(reply["error"]["code"], "UNKNOWN_OPERATION")

    def test_no_session_means_session_unavailable(self):
        with self.assertRaises(SessionUnavailable):
            sessions.SessionRegistry(active_uid=lambda: None).pick()

    def test_active_uid_from_logind(self):
        run = fake_run({("loginctl", "show-seat"): (0, "c2\n"), ("loginctl", "show-session"): (0, "1000\n")})
        self.assertEqual(sessions.active_session_uid(run), 1000)
        self.assertIsNone(sessions.active_session_uid(fake_run({})))


# --- session agent ------------------------------------------------------------------------------

class FakeWinapp:
    def __init__(self):
        self.calls = []
        self.allowed = True

    def list(self):
        return {"applications": [{"id": "com.example.app", "app_state": "INSTALLED"}]}

    def status(self, app_id):
        self.calls.append(("status", app_id))
        return {"application": {"id": app_id, "app_state": "INSTALLED"},
                "policy": {"allowed": self.allowed, "reason": None if self.allowed else "blocked by device policy"}}

    def inspect(self, path, app_id=None, name=None):
        self.calls.append(("inspect", path))
        if path.endswith("x86.exe"):          # boswas-winapp inspect exits 4 with the full decision
            return {"architecture": {"detected": "x86", "supported": False},
                    "decision": {"allowed": False, "reason": "architecture", "message": UNSUPPORTED_32BIT_MESSAGE}}
        return {"decision": {"allowed": True}}

    def install(self, path, app_id=None, name=None, on_line=None, env=None):
        self.calls.append(("install", path, app_id))
        if on_line:
            on_line("boswas-winapp: creating the Wine prefix")
        return {"application": {"id": app_id or "local.setup", "version": "1.0", "state": "installed"}}

    def runtime(self):
        return {"healthy": True, "architectures": ["x86_64"], "apparmor": {"mode": "unknown"}}

    def launch(self, app_id, env=None):
        self.calls.append(("launch", app_id, env))
        return subprocess.Popen([sys.executable, "-c", "import json,time; time.sleep(30)"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)


class SessionAgentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        os.chmod(self.tmp, 0o755)
        self.winapp = FakeWinapp()
        self.agent = session_agent.SessionAgent(socket_path=self.tmp / "boswas/session.sock", winapp=self.winapp,
                                                agent_socket=str(self.tmp / "none.sock"),
                                                environ={"HOME": str(self.tmp)}, run=fake_run({}),
                                                artifacts_prefix="/var/lib/boswas/agent/artifacts/")
        self.agent.start()
        self.addCleanup(self.agent.stop)
        self.client = localapi.ApiClient(self.tmp / "boswas/session.sock", timeout=20)

    def test_gui_operations_go_through_the_backend(self):
        self.assertEqual(self.client.call("apps.list")["applications"][0]["id"], "com.example.app")
        job = self.client.call("apps.install", path=str(self.tmp / "setup.exe"))["job_id"]
        for _ in range(100):
            state = self.client.call("jobs.get", job_id=job)
            if state["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(state["state"], "succeeded")
        self.assertIn("boswas-winapp: creating the Wine prefix", state["progress"])
        self.assertEqual([c[0] for c in self.winapp.calls], ["inspect", "install"])
        self.assertEqual(stat.S_IMODE((self.tmp / "boswas").stat().st_mode), 0o700)

    def test_32_bit_installers_fail_before_installation(self):
        with self.assertRaises(localapi.ApiError) as cm:
            self.client.call("apps.install", path=str(self.tmp / "x86.exe"))
        self.assertEqual(cm.exception.code, "ARCHITECTURE")
        self.assertIn(UNSUPPORTED_32BIT_MESSAGE, cm.exception.message)
        self.assertNotIn("install", [c[0] for c in self.winapp.calls])

    def test_parameter_and_policy_checks(self):
        for op, params in (("apps.install", {"path": "relative/setup.exe"}), ("apps.status", {"id": "../../etc"}),
                           ("apps.launch", {"id": "com.example.app", "display": "remote.host:0"}),
                           ("apps.logs", {"id": "com.example.app", "kind": "../x"})):
            with self.assertRaises(localapi.ApiError) as cm:
                self.client.call(op, **params)
            self.assertEqual(cm.exception.code, "BAD_REQUEST", op)
        self.winapp.allowed = False
        with self.assertRaises(localapi.ApiError) as cm:
            self.client.call("apps.launch", id="com.example.app")
        self.assertEqual(cm.exception.code, "POLICY")
        self.assertFalse(any(c[0] == "launch" for c in self.winapp.calls))
        self.assertTrue(any(e["type"] == "policy-refused" for e in self.client.call("events.list")["events"]))

    def test_remote_installs_only_from_agent_artifacts(self):
        with self.assertRaises(localapi.ApiError):
            self.agent.remote_execute("apps.install", {"id": "com.example.app", "path": "/home/user/evil.exe"})
        with self.assertRaises(localapi.ApiError):
            self.agent.remote_execute("apps.install",
                                      {"id": "com.example.app", "path": "/var/lib/boswas/agent/artifacts/../../x"})
        result = self.agent.remote_execute("apps.install", {"id": "com.example.app",
                                                            "path": "/var/lib/boswas/agent/artifacts/aa-setup.exe"})
        self.assertEqual(result["application"]["id"], "com.example.app")
        jobs = self.client.call("jobs.list")["jobs"]
        self.assertEqual((jobs[0]["source"], jobs[0]["state"]), ("remote", "succeeded"))

    def test_launch_starts_through_boswas_winapp(self):
        result = self.client.call("apps.launch", id="com.example.app", display=":0")
        self.assertTrue(result["started"])
        call = next(c for c in self.winapp.calls if c[0] == "launch")
        self.assertEqual(call[2]["DISPLAY"], ":0")
        with self.agent.lock:
            proc = self.agent.launches["com.example.app"]
        proc.kill()

    def test_system_status_without_the_device_agent(self):
        status = self.client.call("system.status")
        self.assertEqual(status["agent"], {"available": False})
        self.assertEqual(status["windows_architectures"], ["x86_64"])

    @unittest.skipUnless(ROOT, "needs root to act as another user")
    def test_other_users_cannot_use_the_session_agent(self):
        os.chmod(self.tmp / "boswas", 0o755)      # even if the directory were open
        os.chmod(self.tmp / "boswas/session.sock", 0o666)
        code = ("import sys; sys.path[:0] = sys.argv[2:]\n"
                "from boswas_agent.localapi import ApiClient, ApiError\n"
                "try:\n    ApiClient(sys.argv[1]).call('apps.list'); print('ALLOWED')\n"
                "except ApiError as e:\n    print(e.code)\n")
        proc = subprocess.run([sys.executable, "-c", code, str(self.tmp / "boswas/session.sock"),
                               str(HERE.parents[1]), str(HERE.parents[2] / "boswas-compat")],
                              user=OTHER_UID, group=OTHER_UID, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "FORBIDDEN", proc.stderr)


# --- boswas-winapp wrapper --------------------------------------------------------------------------

class WinappWrapperTests(unittest.TestCase):
    def test_argument_lists_and_environment(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout='{"command": "status", "application": {}}', stderr="")
        w = winapp_mod.Winapp(run=run, env={"HOME": "/home/u", "LD_PRELOAD": "/tmp/evil.so", "DISPLAY": ":0",
                                            "PYTHONPATH": "/tmp", "BOSWAS_SYSROOT": "/tmp/fake"})
        w.status("com.example.app")
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["/usr/bin/boswas-winapp", "--json", "status", "com.example.app"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(set(kwargs["env"]), {"PATH", "LC_ALL", "HOME", "DISPLAY"})
        for bad in ("../x", "com.example.app; rm -rf /", "", None):
            with self.assertRaises(BackendError):
                w.status(bad)
        self.assertEqual(len(calls), 1)

    def test_errors_and_sanitising(self):
        def run(argv, **kwargs):
            doc = {"command": argv[2], "error": {"reason": "blocked", "message": "blocked \x1b[31mred\x1b[0m"}}
            return subprocess.CompletedProcess(argv, 4, stdout=json.dumps(doc), stderr="")
        with self.assertRaises(BackendError) as cm:
            winapp_mod.Winapp(run=run, env={}).catalog()
        self.assertEqual((cm.exception.reason, cm.exception.exit_code), ("blocked", 4))
        self.assertNotIn("\x1b", str(cm.exception))

        def refused_inspect(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 4, stdout='{"command": "inspect", "decision": {"allowed": false}}',
                                               stderr="")
        self.assertFalse(winapp_mod.Winapp(run=refused_inspect, env={}).call("inspect", "/x")["decision"]["allowed"])


# --- boswas-device CLI -----------------------------------------------------------------------------

class CliTests(AgentCase):
    managed = False

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), paths=self.paths)
        return code, out.getvalue(), err.getvalue()

    def test_read_only_commands_work_without_the_service(self):
        self.agent.tick()
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("NOT RUNNING", out)
        code, out, _ = self.run_cli("--json", "identity")
        self.assertEqual(json.loads(out)["identity"]["device_id"], self.agent.device_id)
        code, out, _ = self.run_cli("inventory")
        self.assertIn("x86_64 only", out)

    def test_config_validate(self):
        bad = self.root / "bad.conf"
        bad.write_text('CONTROL_PLANE_URL="http://x"\nAPI_TOKEN="abc"\n')
        code, out, _ = self.run_cli("config", "validate", "--file", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("secret-looking key", out)
        code, _, _ = self.run_cli("config", "validate", "--file", str(HERE.parents[3] / "config/boswas/device.conf"))
        self.assertEqual(code, 0)

    def test_service_operations_need_the_service_and_tokens_never_come_from_argv(self):
        code, _, err = self.run_cli("sync")
        self.assertEqual(code, 5)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["enroll", "--token", TOKEN], paths=self.paths)
        code, out, _ = self.run_cli("--json", "unenroll")
        self.assertEqual((code, json.loads(out)["error"]["reason"]), (2, "usage"))


class StaticSafetyTests(unittest.TestCase):
    def test_no_shell_execution_anywhere_in_the_agent(self):
        source = HERE.parents[1] / "boswas_agent"
        for path in source.glob("*.py"):
            text = path.read_text()
            for pattern in ("shell=True", "os.system(", "os.popen(", "eval(", "exec(", "pickle", "__import__("):
                self.assertNotIn(pattern, text, f"{path.name}: {pattern}")


if __name__ == "__main__":
    unittest.main()
