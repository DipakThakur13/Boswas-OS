"""Control Plane domain rules: enrollment, registry, heartbeat, inventory,
typed commands and their lifecycle, catalog, artifacts, policies, audit trail.

Run: python3 -m unittest discover -s tests   (needs the openssl command)
"""

import base64
import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cp_testlib import ADMIN, OPERATOR, ControlPlaneCase, enrollment_request, inventory_doc, manifest, pe_bytes  # noqa: E402

from boswas_agent import commands, policydoc  # noqa: E402
from boswas_agent.models import EventType  # noqa: E402
from boswas_cp import auth  # noqa: E402
from boswas_cp.pki import certificate_der  # noqa: E402
from boswas_cp.service import ServiceError  # noqa: E402


def code_of(fn):
    try:
        fn()
    except ServiceError as exc:
        return exc.code
    return None


class SetupTests(ControlPlaneCase):
    def test_default_policy_is_signed_and_keys_are_private(self):
        policy = self.cp.current_policy("default")
        envelope = json.loads(policy["envelope"])
        doc = policydoc.open_envelope(envelope, self.pki.policy_public_key())
        self.assertEqual((doc["version"], doc["compat"]["unlisted_apps"]), ("default-1", "deny"))
        for key in (self.pki.server_ca_key, self.pki.device_ca_key, self.pki.server_key, self.pki.policy_key):
            self.assertEqual(key.stat().st_mode & 0o777, 0o600, key)
        self.cp.ensure_default_policy()                       # idempotent
        self.assertEqual(self.cp.current_policy("default")["sequence"], 1)


class EnrollmentTests(ControlPlaneCase):
    def test_enrollment_issues_a_client_certificate_for_the_device_id(self):
        req, res = self.enroll()
        self.assertEqual(res["device_id"], req["device_id"])
        subject = subprocess.run(["openssl", "x509", "-noout", "-subject", "-ext", "extendedKeyUsage,basicConstraints",
                                  "-nameopt", "RFC2253"], input=res["certificate_pem"], capture_output=True,
                                 text=True).stdout
        self.assertIn(f"CN={req['device_id']}", subject)
        self.assertNotIn("whatever-the-device-says", subject)
        self.assertIn("TLS Web Client Authentication", subject)
        self.assertIn("CA:FALSE", subject)
        self.assertEqual(res["policy_version"], "default-1")
        self.assertIn("BEGIN PUBLIC KEY", res["policy_public_key_pem"])
        device = self.device(req["device_id"])
        self.assertEqual((device["status"], device["connection"], device["name"]), ("active", "never", "Test Laptop"))
        self.assertEqual(len(self.events("DEVICE_REGISTERED")), 1)

    def test_tokens_are_single_use_and_bounded(self):
        token = self.token()
        self.cp.enroll(enrollment_request(token, self.tmp))
        self.assertEqual(code_of(lambda: self.cp.enroll(enrollment_request(token, self.tmp))), "INVALID_TOKEN")
        expired = self.token(ttl_hours=1)
        self.clock[0] += 7200
        self.assertEqual(code_of(lambda: self.cp.enroll(enrollment_request(expired, self.tmp))), "INVALID_TOKEN")
        revoked = self.cp.create_enrollment_token(ADMIN)
        self.cp.revoke_enrollment_token(ADMIN, revoked["token_id"])
        self.assertEqual(code_of(lambda: self.cp.enroll(enrollment_request(revoked["token"], self.tmp))),
                         "INVALID_TOKEN")
        forged = revoked["token"][:-4] + ("BBBB" if revoked["token"].endswith("AAAA") else "AAAA")
        self.assertEqual(code_of(lambda: self.cp.enroll(enrollment_request(forged, self.tmp))), "INVALID_TOKEN")
        self.assertGreaterEqual(len(self.events("SECURITY_EVENT")), 3)          # refusals are audited
        multi = self.token(max_uses=2)
        self.cp.enroll(enrollment_request(multi, self.tmp))
        self.cp.enroll(enrollment_request(multi, self.tmp))

    def test_secrets_are_never_stored_in_clear(self):
        token = self.cp.create_enrollment_token(ADMIN)["token"]
        op = self.cp.add_operator("test", "alice", "operator")["token"]
        dump = "\n".join(self.store.connection().iterdump())
        for secret in (token, op, token.split("_", 2)[2], op.split("_", 2)[2]):
            self.assertNotIn(secret, dump)
        self.cp.enroll(enrollment_request(token, self.tmp))
        self.assertNotIn(token, json.dumps(self.cp.events(limit=1000)))

    def test_invalid_requests(self):
        token = self.token()
        self.assertEqual(code_of(lambda: self.cp.enroll(enrollment_request(token, self.tmp, keystrokes="x"))),
                         "INVALID_REQUEST")
        self.assertEqual(code_of(lambda: self.cp.enroll(enrollment_request(token, self.tmp, device_id="my-laptop"))),
                         "INVALID_DEVICE_ID")
        self.assertEqual(code_of(lambda: self.cp.enroll(enrollment_request(token, self.tmp, csr_pem=
                         "-----BEGIN CERTIFICATE REQUEST-----\nAAAA\n-----END CERTIFICATE REQUEST-----\n"))),
                         "INVALID_CSR")
        self.assertEqual(code_of(lambda: self.cp.enroll(enrollment_request(token, self.tmp, ephemeral=True))),
                         "EPHEMERAL_NOT_ALLOWED")
        ok = self.token(allow_ephemeral=True)
        self.assertTrue(self.device(self.cp.enroll(enrollment_request(ok, self.tmp, ephemeral=True))["device_id"])
                        ["ephemeral"])

    def test_reenrollment_revokes_the_old_certificate_and_retired_devices_stay_out(self):
        req, first = self.enroll()
        device_id = req["device_id"]
        # Any valid token is not enough to take over an enrolled device's identity.
        self.assertEqual(code_of(lambda: self.cp.enroll(enrollment_request(self.token(), self.tmp,
                                                                           device_id=device_id))),
                         "DEVICE_ALREADY_ENROLLED")
        other_device = self.token(device_id=str(uuid.uuid4()))
        self.assertEqual(code_of(lambda: self.cp.enroll(enrollment_request(other_device, self.tmp,
                                                                           device_id=device_id))), "INVALID_TOKEN")
        self.assertEqual(self.cp.device_for_certificate(certificate_der(first["certificate_pem"]), device_id)
                         ["device_id"], device_id)                       # refusals changed nothing
        self.assertEqual(code_of(lambda: self.cp.create_enrollment_token(ADMIN, device_id="laptop-7")),
                         "INVALID_DEVICE_ID")
        second = self.cp.enroll(enrollment_request(self.token(device_id=device_id), self.tmp, device_id=device_id))
        self.assertEqual(code_of(lambda: self.cp.device_for_certificate(certificate_der(first["certificate_pem"]),
                                                                        device_id)), "CERTIFICATE_REVOKED")
        self.assertEqual(self.cp.device_for_certificate(certificate_der(second["certificate_pem"]), device_id)
                         ["device_id"], device_id)
        other, _ = self.enroll()
        self.assertEqual(code_of(lambda: self.cp.device_for_certificate(certificate_der(second["certificate_pem"]),
                                                                        other["device_id"])), "DEVICE_MISMATCH")
        self.cp.retire_device(ADMIN, device_id)
        self.assertEqual(code_of(lambda: self.cp.device_for_certificate(certificate_der(second["certificate_pem"]),
                                                                        device_id)), "DEVICE_RETIRED")
        self.assertEqual(code_of(lambda: self.cp.enroll(enrollment_request(self.token(), self.tmp,
                                                                           device_id=device_id))), "DEVICE_RETIRED")
        self.assertEqual(code_of(lambda: self.cp.device_for_certificate(None, device_id)), "CERTIFICATE_REQUIRED")


class DeviceTrafficTests(ControlPlaneCase):
    def setUp(self):
        super().setUp()
        req, _ = self.enroll()
        self.device_id = req["device_id"]

    def test_heartbeat_registry_and_online_offline(self):
        res = self.cp.heartbeat(self.device(self.device_id), self.heartbeat(self.device_id))
        self.assertEqual((res["accepted"], res["policy_version"], res["inventory_requested"]), (True, "default-1", True))
        row = self.device(self.device_id)
        self.assertEqual((row["connection"], row["device_state"], row["agent_version"]), ("online", "READY", "1.0~alpha3"))
        self.assertEqual(len(self.events("DEVICE_ONLINE")), 1)
        self.cp.store_report(self.device(self.device_id), "inventory", inventory_doc(self.device_id))
        res = self.cp.heartbeat(self.device(self.device_id), self.heartbeat(self.device_id))
        self.assertFalse(res["inventory_requested"])
        self.assertEqual(len(self.events("DEVICE_ONLINE")), 1)          # no repeated online events
        self.clock[0] += 3600
        self.assertEqual(self.cp.sweep()["offline"], 1)
        self.assertEqual(self.device(self.device_id)["connection"], "offline")
        self.assertEqual(len(self.events("DEVICE_OFFLINE")), 1)
        self.cp.heartbeat(self.device(self.device_id), self.heartbeat(self.device_id))
        self.assertEqual(len(self.events("DEVICE_ONLINE")), 2)

    def test_unexpected_or_foreign_documents_are_refused(self):
        dev = self.device(self.device_id)
        self.assertEqual(code_of(lambda: self.cp.heartbeat(dev, {**self.heartbeat(self.device_id), "hostname": "x"})),
                         "PRIVACY_VIOLATION")
        self.assertEqual(code_of(lambda: self.cp.heartbeat(dev, self.heartbeat(str(uuid.uuid4())))), "DEVICE_MISMATCH")
        inv = inventory_doc(self.device_id)
        inv["hardware"] = {"serial": "XYZ"}
        self.assertEqual(code_of(lambda: self.cp.store_report(dev, "inventory", inv)), "PRIVACY_VIOLATION")
        self.assertIsNone(self.cp.report(self.device_id, "inventory"))

    def test_inventory_and_applications_view(self):
        self.catalog_app(version="2.0")
        self.cp.store_report(self.device(self.device_id), "inventory", inventory_doc(self.device_id))
        apps = self.cp.device_applications(self.device_id)["applications"]
        self.assertEqual((apps[0]["id"], apps[0]["update_available"], apps[0]["catalog"]["version"]),
                         ("com.example.app", True, "2.0"))
        self.assertEqual(self.device(self.device_id)["architecture"], "amd64")
        self.assertEqual(self.cp.summary()["applications"]["running"], 1)

    def test_device_events_are_audited_with_their_origin(self):
        self.cp.device_events(self.device(self.device_id), {
            "schema": "boswas-device-event/1", "device_id": self.device_id,
            "events": [{"type": "APPLICATION_BLOCKED", "occurred_at": "2026-10-04T12:00:00Z", "source": "remote",
                        "detail": "blocked", "application_id": "com.example.app"},
                       {"type": "SECURITY_EVENT", "occurred_at": "garbage", "source": "local", "detail": "x"}]})
        blocked = self.events("APPLICATION_BLOCKED")[0]
        self.assertEqual((blocked["source"], blocked["actor"]), ("device", f"device:{self.device_id}"))
        self.assertEqual(code_of(lambda: self.cp.device_events(self.device(self.device_id), {
            "schema": "boswas-device-event/1", "device_id": self.device_id,
            "events": [{"type": "DEVICE_REGISTERED", "occurred_at": "2026-10-04T12:00:00Z", "source": "local",
                        "detail": "forged"}]})), "PRIVACY_VIOLATION")


class CommandTests(ControlPlaneCase):
    def setUp(self):
        super().setUp()
        req, _ = self.enroll()
        self.device_id = req["device_id"]

    def create(self, **body):
        return self.cp.create_command(OPERATOR, self.device_id, body)

    def test_command_lifecycle_and_audit(self):
        app = self.catalog_app()
        cmd, created = self.create(type="INSTALL_APPLICATION", application_id="com.example.app")
        self.assertTrue(created)
        self.assertEqual(cmd["status"], "QUEUED")
        self.assertEqual(cmd["payload"]["installer"]["sha256"], app["installer"]["sha256"])
        dev = self.device(self.device_id)
        docs = self.cp.pending_commands(dev)["commands"]
        self.assertEqual(len(docs), 1)
        parsed = commands.parse_command(docs[0], device_id=self.device_id, now=self.cp.now())   # the device's check
        self.assertEqual(parsed.payload["manifest"]["id"], "com.example.app")
        self.assertEqual(self.cp.get_command(self.device_id, cmd["command_id"])["status"], "SENT")
        self.assertEqual(len(self.cp.pending_commands(dev)["commands"]), 1)           # redelivered until acked
        self.cp.acknowledge(dev, cmd["command_id"], {"status": "ACKNOWLEDGED"})
        self.assertEqual(self.cp.pending_commands(dev)["commands"], [])
        self.cp.acknowledge(dev, cmd["command_id"], {"status": "RUNNING"})
        result = commands.result_document(cmd["command_id"], self.device_id,
                                          commands.CommandOutcome.ok(application_id="com.example.app", installed=True))
        self.cp.command_result(dev, cmd["command_id"], result)
        final = self.cp.get_command(self.device_id, cmd["command_id"])
        self.assertEqual((final["status"], final["result"]["installed"]), ("SUCCEEDED", True))
        self.assertEqual(code_of(lambda: self.cp.command_result(dev, cmd["command_id"], result)), None)   # idempotent
        self.assertEqual(len(self.events("COMMAND_CREATED")), 1)
        self.assertEqual(len(self.events("COMMAND_COMPLETED")), 1)
        self.assertEqual(self.events("COMMAND_CREATED")[0]["actor"], "operator:ops")

    def test_only_typed_commands_exist(self):
        self.assertEqual(code_of(lambda: self.create(type="EXECUTE_SHELL_COMMAND")), "UNSUPPORTED_COMMAND")
        self.assertEqual(code_of(lambda: self.create(type="LAUNCH_APPLICATION", application_id="com.example.app",
                                                     command="calc.exe")), "INVALID_REQUEST")
        self.assertEqual(code_of(lambda: self.create(type="LAUNCH_APPLICATION", application_id="../../bin/sh")),
                         "INVALID_PAYLOAD")
        self.assertEqual(code_of(lambda: self.create(type="UPDATE_AGENT", version="1.0; rm -rf /")), "INVALID_PAYLOAD")
        self.assertEqual(code_of(lambda: self.create(type="INSTALL_APPLICATION", application_id="com.none.app")),
                         "NOT_IN_CATALOG")
        self.assertEqual(self.store.one("SELECT COUNT(*) AS n FROM commands")["n"], 0)

    def test_32_bit_catalog_entries_never_become_commands(self):
        app = self.catalog_app(app_id="com.legacy.app", machine=0x14C, architecture="x86")
        self.assertEqual((app["support"], app["installable"]), ("UNSUPPORTED_ARCHITECTURE", False))
        self.assertEqual(app["architecture_label"], "x86 / 32-bit (not supported)")
        self.assertEqual(code_of(lambda: self.create(type="INSTALL_APPLICATION", application_id="com.legacy.app")),
                         "UNSUPPORTED_ARCHITECTURE")
        self.assertEqual(code_of(lambda: self.catalog_app(app_id="com.liar.app", machine=0x14C)),
                         "ARCHITECTURE_MISMATCH")

    def test_idempotency_and_duplicates(self):
        a, created_a = self.create(type="REFRESH_INVENTORY", idempotency_key="nightly-1")
        b, created_b = self.create(type="REFRESH_INVENTORY", idempotency_key="nightly-1")
        self.assertEqual((a["command_id"], created_a, created_b), (b["command_id"], True, False))
        self.assertEqual(code_of(lambda: self.create(type="LAUNCH_APPLICATION", application_id="com.example.app",
                                                     idempotency_key="nightly-1")), "IDEMPOTENCY_KEY_REUSED")
        c, created_c = self.create(type="REFRESH_INVENTORY")              # same pending operation
        self.assertEqual((c["command_id"], created_c), (a["command_id"], False))

    def test_expiry_cancellation_and_stale_results(self):
        cmd, _ = self.create(type="REFRESH_INVENTORY", ttl_seconds=120)
        self.assertEqual(code_of(lambda: self.create(type="LAUNCH_APPLICATION", application_id="com.example.app",
                                                     ttl_seconds=10)), "INVALID_TTL")
        self.clock[0] += 300
        self.assertEqual(self.cp.sweep()["expired"], 1)
        self.assertEqual(self.cp.get_command(self.device_id, cmd["command_id"])["status"], "EXPIRED")
        self.assertEqual(self.cp.pending_commands(self.device(self.device_id))["commands"], [])
        late = commands.result_document(cmd["command_id"], self.device_id, commands.CommandOutcome.ok())
        self.assertEqual(code_of(lambda: self.cp.command_result(self.device(self.device_id), cmd["command_id"], late)),
                         "COMMAND_NOT_ACTIVE")
        queued, _ = self.create(type="LAUNCH_APPLICATION", application_id="com.example.app")
        self.assertEqual(self.cp.cancel_command(OPERATOR, self.device_id, queued["command_id"])["status"], "CANCELLED")
        sent, _ = self.create(type="STOP_APPLICATION", application_id="com.example.app")
        self.cp.pending_commands(self.device(self.device_id))
        self.assertEqual(code_of(lambda: self.cp.cancel_command(OPERATOR, self.device_id, sent["command_id"])),
                         "NOT_CANCELLABLE")
        self.cp.acknowledge(self.device(self.device_id), sent["command_id"], {"status": "RUNNING"})
        self.clock[0] += 2 * 86400
        self.cp.sweep()
        stale = self.cp.get_command(self.device_id, sent["command_id"])
        self.assertEqual((stale["status"], stale["error"]["code"]), ("FAILED", "NO_RESULT"))

    def test_results_only_from_the_target_device(self):
        cmd, _ = self.create(type="REFRESH_INVENTORY")
        other, _ = self.enroll()
        result = commands.result_document(cmd["command_id"], other["device_id"], commands.CommandOutcome.ok())
        self.assertEqual(code_of(lambda: self.cp.command_result(self.device(other["device_id"]), cmd["command_id"],
                                                                result)), "NOT_FOUND")

    def test_policy_limits_commands_and_retired_devices_get_none(self):
        doc = policydoc.default_policy()
        doc["agent"]["allowed_commands"] = ["REFRESH_INVENTORY"]
        self.cp.publish_policy(ADMIN, {k: doc[k] for k in ("name", "description", "compat", "agent", "updates")})
        self.assertEqual(code_of(lambda: self.create(type="LAUNCH_APPLICATION", application_id="com.example.app")),
                         "COMMAND_NOT_ALLOWED")
        apply_cmd = None
        self.cp.retire_device(ADMIN, self.device_id)
        self.assertEqual(code_of(lambda: self.create(type="REFRESH_INVENTORY")), "DEVICE_RETIRED")
        self.assertIsNone(apply_cmd)


class CatalogAndPolicyTests(ControlPlaneCase):
    def test_catalog_validation(self):
        exe = self.tmp / "tool.exe"
        exe.write_bytes(pe_bytes())
        sha = self.cp.import_artifact_file(ADMIN, exe)["sha256"]
        self.assertEqual(code_of(lambda: self.cp.upsert_application(ADMIN, {"manifest": manifest(sha="b" * 64)})),
                         "ARTIFACT_MISSING")
        self.assertEqual(code_of(lambda: self.cp.upsert_application(ADMIN, {"manifest": manifest(
            sha=sha, environment={"LD_PRELOAD": "/tmp/x.so"})})), "INVALID_MANIFEST")
        unpinned = manifest(sha=sha)
        unpinned["status"] = "experimental"
        unpinned["installer"] = {"type": "exe"}
        self.assertEqual(code_of(lambda: self.cp.upsert_application(ADMIN, {"manifest": unpinned})),
                         "INSTALLER_NOT_PINNED")
        app = self.cp.upsert_application(ADMIN, {"manifest": manifest(sha=sha)})
        self.assertTrue(app["installable"])
        self.assertFalse(self.cp.deprecate_application(ADMIN, "com.example.app")["installable"])
        notexe = self.tmp / "readme.txt"
        notexe.write_text("hello")
        self.assertEqual(code_of(lambda: self.cp.import_artifact_file(ADMIN, notexe)), "NOT_AN_INSTALLER")
        self.assertEqual(len(self.events("CATALOG_UPDATED")), 2)

    def test_policy_publication_and_validation(self):
        body = {k: policydoc.default_policy()[k] for k in ("name", "description", "compat", "agent", "updates")}
        body["compat"] = {**body["compat"], "unlisted_apps": "allow"}
        published = self.cp.publish_policy(ADMIN, body)
        self.assertEqual(published["version"], "default-2")
        self.assertEqual([h["version"] for h in published["history"]], ["default-2", "default-1"])
        bad = {**body, "compat": {**body["compat"], "require_apparmor": False}}
        self.assertEqual(code_of(lambda: self.cp.publish_policy(ADMIN, bad)), "INVALID_POLICY")
        bad = {**body, "agent": {**body["agent"], "allowed_commands": ["EXECUTE_SHELL_COMMAND"]}}
        self.assertEqual(code_of(lambda: self.cp.publish_policy(ADMIN, bad)), "INVALID_POLICY")
        self.assertEqual(code_of(lambda: self.cp.publish_policy(ADMIN, {**body, "sequence": 99})), "INVALID_REQUEST")
        self.assertEqual(self.cp.current_policy("default")["version"], "default-2")
        req, _ = self.enroll()
        envelope = self.cp.policy_envelope(self.device(req["device_id"]))
        self.assertEqual(policydoc.open_envelope(envelope, self.pki.policy_public_key())["version"], "default-2")
        self.assertEqual(code_of(lambda: self.cp.update_device(ADMIN, req["device_id"], {"policy_name": "missing"})),
                         "NO_SUCH_POLICY")

    def test_policy_status_in_the_summary(self):
        req, _ = self.enroll()
        device_id = req["device_id"]
        policies = lambda: self.cp.summary()["policies"]   # noqa: E731
        self.cp.heartbeat(self.device(device_id), self.heartbeat(device_id, policy_version=None))
        self.assertEqual((policies()["devices_outdated"], policies()["devices_unreported"]), (0, 1))
        self.cp.heartbeat(self.device(device_id), self.heartbeat(device_id))
        self.assertEqual((policies()["devices_outdated"], policies()["devices_unreported"]), (0, 0))
        body = {k: policydoc.default_policy()[k] for k in ("name", "description", "compat", "agent", "updates")}
        self.cp.publish_policy(ADMIN, body)                                   # default-2; the device has default-1
        self.assertEqual((policies()["devices_outdated"], policies()["devices_unreported"]), (1, 0))

    def test_summary_finds_security_events_beyond_the_newest_events(self):
        req, _ = self.enroll()
        self.cp.record(EventType.SECURITY_EVENT, actor="test", device_id=req["device_id"],
                       detail={"action": "test security event"})
        for _ in range(250):                                                  # a busy fleet
            self.cp.record(EventType.DEVICE_UPDATED, actor="test", device_id=req["device_id"], detail={})
        security = self.cp.summary()["security_events"]
        self.assertIn("test security event", [e["detail"].get("action") for e in security])

    def test_retired_devices_are_read_only_and_cancellations_are_not_failures(self):
        req, _ = self.enroll()
        device_id = req["device_id"]
        cmd, _ = self.cp.create_command(OPERATOR, device_id, {"type": "REFRESH_INVENTORY"})
        self.cp.cancel_command(OPERATOR, device_id, cmd["command_id"])
        self.assertEqual(len(self.events("COMMAND_CANCELLED")), 1)
        self.assertEqual(self.events("COMMAND_FAILED"), [])
        self.cp.retire_device(ADMIN, device_id)
        name = self.device(device_id)["name"]
        self.assertEqual(code_of(lambda: self.cp.update_device(ADMIN, device_id, {"name": "renamed"})),
                         "DEVICE_RETIRED")
        self.assertEqual(self.device(device_id)["name"], name)
        self.cp.add_operator("test", "carol", "viewer")
        self.cp.disable_operator("test", "carol")
        self.assertEqual([o["disabled"] for o in self.cp.list_operators()], [True])      # booleans, like tokens


class AuditTests(ControlPlaneCase):
    def test_hash_chain_detects_tampering(self):
        self.enroll()
        self.cp.record("SECURITY_EVENT", actor="test", detail={"note": "x"})
        self.assertTrue(self.cp.verify_events()["valid"])
        with self.store.transaction() as conn:
            conn.execute("UPDATE events SET detail = '{\"note\": \"edited\"}' WHERE type = 'SECURITY_EVENT'")
        result = self.cp.verify_events()
        self.assertFalse(result["valid"])
        self.assertIsNotNone(result["first_invalid_event"])

    def test_event_details_never_carry_secrets(self):
        event = self.cp.record("SECURITY_EVENT", actor="test", detail={
            "token": "bet_x", "password": "p", "nested": {"private_key": "k", "ok": "v\x1b[2J"}, "api_secret": 1})
        self.assertEqual(event["detail"], {"nested": {"ok": "v[2J"}})


class OperatorAuthTests(ControlPlaneCase):
    def test_tokens_roles_and_disabling(self):
        token = self.cp.add_operator("test", "viewer1", "viewer")["token"]
        authn = auth.LocalTokenAuthenticator(self.store)
        principal = authn.authenticate(f"Bearer {token}")
        self.assertEqual((principal.actor, principal.role), ("operator:viewer1", "viewer"))
        self.assertTrue(principal.allows("viewer"))
        self.assertFalse(principal.allows("operator"))
        tampered = token[:-1] + ("B" if token.endswith("A") else "A")
        for header in (None, "", "Basic abc", f"Bearer {token}x", f"Bearer {tampered}", "Bearer bet_" + token[4:]):
            self.assertIsNone(authn.authenticate(header), header)
        self.cp.disable_operator("test", "viewer1")
        self.assertIsNone(authn.authenticate(f"Bearer {token}"))
        self.assertEqual(code_of(lambda: self.cp.add_operator("test", "Bad Name", "admin")), "INVALID_NAME")
        self.assertEqual(code_of(lambda: self.cp.add_operator("test", "root2", "superuser")), "INVALID_ROLE")

    def test_failure_limiter(self):
        t = [0.0]
        limiter = auth.FailureLimiter(limit=3, window=60, clock=lambda: t[0])
        for _ in range(3):
            limiter.failure("10.0.0.1")
        self.assertTrue(limiter.blocked("10.0.0.1"))
        self.assertFalse(limiter.blocked("10.0.0.2"))
        t[0] = 61
        self.assertFalse(limiter.blocked("10.0.0.1"))


if __name__ == "__main__":
    unittest.main()
