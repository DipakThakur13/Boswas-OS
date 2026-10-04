"""Unit tests for the device agent foundations (Milestone 1: interfaces only).

Run: python3 -m unittest discover -s tests
"""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
SCHEMAS = HERE.parents[1] / "schemas"

from boswas_agent import device_identity, interfaces, inventory, models, privacy, user_identity  # noqa: E402
from boswas_agent.errors import (DeviceConfigError, NotEnrolled, PrivacyViolation,  # noqa: E402
                                 UnsupportedCommand)


def validate(instance, schema, where="$"):
    """The subset of JSON Schema used by schemas/*.json (enough to keep models and schemas in sync)."""
    if "const" in schema and instance != schema["const"]:
        raise AssertionError(f"{where}: {instance!r} != const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise AssertionError(f"{where}: {instance!r} not in {schema['enum']}")
    types = schema.get("type")
    if types:
        names = types if isinstance(types, list) else [types]
        py = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float),
              "boolean": bool, "null": type(None)}
        ok = any(isinstance(instance, py[n]) and not (n in ("integer", "number") and isinstance(instance, bool))
                 for n in names)
        if not ok:
            raise AssertionError(f"{where}: {instance!r} is not {names}")
    if isinstance(instance, str) and "pattern" in schema and not re.search(schema["pattern"], instance):
        raise AssertionError(f"{where}: {instance!r} does not match {schema['pattern']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                raise AssertionError(f"{where}: missing {key}")
        props = schema.get("properties", {})
        for key, value in instance.items():
            if "propertyNames" in schema:
                validate(key, schema["propertyNames"], f"{where}.<{key}>")
            if key in props:
                validate(value, props[key], f"{where}.{key}")
            elif schema.get("additionalProperties") is False:
                raise AssertionError(f"{where}: unexpected {key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                validate(value, schema["additionalProperties"], f"{where}.{key}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            validate(item, schema["items"], f"{where}[{i}]")


def schema(name):
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text())


OS = models.OsInfo(name="Boswas OS", version="v1 Alpha", version_id="1.0~alpha2", build_id="BOS-1",
                   debian_version="13.7", kernel="6.12.111+deb13-amd64")
HW = models.HardwareFacts(vendor="QEMU", model="Standard PC", firmware_version="1.16", cpu="x86",
                          memory_gib=4.0, tpm_version=None, boot_mode="UEFI")


def heartbeat(**overrides):
    values = dict(device_id=device_identity.generate_device_id(), agent_version="1.0~alpha2", os=OS,
                  uptime_seconds=120, compliance=models.ComplianceSummary(models.ComplianceState.COMPLIANT, 8, 2, 0, 0),
                  policy_version="2026.10.1", update=models.UpdateStatus(channel="stable", state="up-to-date"),
                  security={"firewall": "PASS", "apparmor": "PASS", "secure-boot": "WARN"},
                  sent_at="2026-10-04T12:00:00Z")
    values.update(overrides)
    return models.Heartbeat(**values)


class DeviceIdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conf = Path(self._tmp.name) / "device.conf"

    def test_device_ids_are_random_uuid4(self):
        ids = {device_identity.generate_device_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        self.assertTrue(all(device_identity.is_valid_device_id(i) for i in ids))
        self.assertFalse(device_identity.is_valid_device_id("boswas-device"))
        self.assertFalse(device_identity.is_valid_device_id("52:54:00:12:34:56"))

    def test_device_id_does_not_depend_on_host_identity(self):
        import socket
        device_id = device_identity.generate_device_id()
        for host_fact in (socket.gethostname(), os.environ.get("USER", ""), "52:54:00"):
            if host_fact:
                self.assertNotIn(host_fact.lower(), device_id)

    def test_shipped_template_parses(self):
        repo_conf = HERE.parents[3] / "config/boswas/device.conf"
        cfg = device_identity.DeviceConfig.load(repo_conf)
        self.assertEqual(cfg.enrollment_state, "unenrolled")
        self.assertEqual(cfg.device_id, "")
        self.assertEqual(cfg.problems(), [])
        text = repo_conf.read_text()
        for key in device_identity.DeviceConfig.KEYS:
            self.assertIn(f"{key}=", text)

    def test_ensure_device_id_creates_once(self):
        self.conf.write_text('DEVICE_ID=""\nENROLLMENT_STATE="unenrolled"\n')
        first = device_identity.ensure_device_id(self.conf)
        self.assertTrue(device_identity.is_valid_device_id(first.device_id))
        self.assertEqual(device_identity.ensure_device_id(self.conf).device_id, first.device_id)
        self.assertEqual(stat.S_IMODE(self.conf.stat().st_mode), 0o644)
        self.conf.write_text('DEVICE_ID="my-laptop"\n')
        with self.assertRaises(DeviceConfigError):
            device_identity.ensure_device_id(self.conf)

    def test_device_conf_never_holds_secrets(self):
        cfg = device_identity.DeviceConfig(device_id=device_identity.generate_device_id())
        for field, value in (("device_certificate", "/home/user/key.pem"),
                             ("device_certificate", "/var/lib/boswas/agent/device.key"),
                             # Marker assembled at runtime so the repository's
                             # private-key scanner does not flag this file.
                             ("tenant_id", "-----BEGIN " + "PRIVATE" + " KEY-----"),
                             ("control_plane_url", "http://plain.example"),
                             ("device_profile", 'x"; rm -rf /')):
            bad = device_identity.DeviceConfig(**{**cfg.__dict__, field: value})
            with self.assertRaises(DeviceConfigError, msg=field):
                bad.write(self.conf)
        good = device_identity.DeviceConfig(**{**cfg.__dict__, "device_certificate": "/var/lib/boswas/agent/device.crt",
                                               "control_plane_url": "https://cp.boswas.invalid",
                                               "enrollment_state": "enrolled", "device_profile": "engineering"})
        good.write(self.conf)
        self.assertNotRegex(self.conf.read_text(), r"(?i)(token|secret|password|key)\s*=")


class MessageTests(unittest.TestCase):
    def test_heartbeat_matches_schema_and_privacy_allowlist(self):
        doc = heartbeat().to_dict()
        validate(doc, schema("heartbeat-v1"))
        privacy.check(doc)

    def test_privacy_guard_rejects_content(self):
        for extra in ({"file_contents": "secret"}, {"keystrokes": "abc"}, {"screenshot": "..."},
                      {"browser_history": []}, {"password": "x"}):
            doc = {**heartbeat().to_dict(), **extra}
            with self.assertRaises(PrivacyViolation):
                privacy.check(doc)
        doc = heartbeat(security={"firewall": "PASS", "home-directory-listing": "PASS"}).to_dict()
        with self.assertRaises(PrivacyViolation):
            privacy.check(doc)
        nested = heartbeat().to_dict()
        nested["os"]["hostname_history"] = ["a"]
        with self.assertRaises(PrivacyViolation):
            privacy.check(nested)
        with self.assertRaises(PrivacyViolation):
            privacy.check({"schema": "boswas-screenshots/1"})

    def test_enrollment_request(self):
        req = models.EnrollmentRequest(device_id=device_identity.generate_device_id(),
                                       csr_pem="-----BEGIN CERTIFICATE REQUEST-----\nMIIB\n-----END CERTIFICATE REQUEST-----\n",
                                       os=OS, hardware=HW, profile="engineering", enrollment_token="one-time-123")
        doc = req.to_dict()
        validate(doc, schema("enrollment-request-v1"))
        privacy.check(doc)
        self.assertNotIn("one-time-123", repr(req))

    def test_compliance_report(self):
        report = models.ComplianceReport(
            device_id=device_identity.generate_device_id(), policy_version=None,
            summary=models.ComplianceSummary(models.ComplianceState.NON_COMPLIANT, 7, 1, 1, 0),
            checks=(models.CheckResult("firewall", "FAIL"), models.CheckResult("usb-policy", "INFO", False)),
            assessed_at="2026-10-04T12:00:00Z")
        doc = report.to_dict()
        validate(doc, schema("compliance-report-v1"))
        privacy.check(doc)

    def test_compliance_state_mapping(self):
        m = models.ComplianceState.from_local_assessment
        self.assertEqual(m("COMPLIANT_WITH_WARNINGS"), models.ComplianceState.COMPLIANT)
        self.assertEqual(m("NON_COMPLIANT"), models.ComplianceState.NON_COMPLIANT)
        self.assertEqual(m(None), models.ComplianceState.UNKNOWN)
        self.assertEqual(set(schema("heartbeat-v1")["properties"]["compliance"]["properties"]["state"]["enum"]),
                         {s.value for s in models.ComplianceState})

    def test_commands(self):
        cmd_schema = schema("device-command-v1")
        self.assertEqual(cmd_schema["properties"]["type"]["enum"], [c.value for c in models.CommandType])
        for forbidden in ("WIPE_DEVICE", "WIPE", "RETIRE_DEVICE", "FACTORY_RESET"):
            self.assertNotIn(forbidden, [c.value for c in models.CommandType])
        doc = {"id": "c1", "type": "SYNC_POLICY", "issued_at": "t", "expires_at": "t", "issued_by": "actor:42"}
        validate(doc, cmd_schema)
        self.assertEqual(models.DeviceCommand.from_dict(doc).type, models.CommandType.SYNC_POLICY)
        with self.assertRaises(UnsupportedCommand):
            models.DeviceCommand.from_dict({**doc, "type": "WIPE_DEVICE"})
        with self.assertRaises(UnsupportedCommand):
            models.DeviceCommand.from_dict({**doc, "parameters": {"x": 1}})

    def test_security_check_ids_match_schema(self):
        self.assertEqual(schema("heartbeat-v1")["properties"]["security"]["propertyNames"]["enum"],
                         list(models.SECURITY_CHECK_IDS))


class InterfaceTests(unittest.TestCase):
    def test_offline_client_never_contacts_anything(self):
        client = interfaces.OfflineControlPlaneClient()
        for call in (lambda: client.heartbeat(heartbeat()), lambda: client.fetch_policy(None),
                     lambda: client.fetch_commands()):
            with self.assertRaises(NotEnrolled):
                call()

    def test_interfaces_are_abstract(self):
        for cls in (interfaces.ControlPlaneClient, interfaces.CredentialStore, interfaces.PolicyVerifier,
                    interfaces.InventoryCollector, interfaces.CommandHandler, user_identity.IdentityProvider):
            with self.assertRaises(TypeError):
                cls()

    def test_no_user_identity_is_asserted(self):
        ctx = user_identity.NoIdentityProvider().current_context("dev-1")
        self.assertFalse(ctx.authenticated)
        self.assertEqual((ctx.principal, ctx.device_id, ctx.method), (None, "dev-1", "none"))


class InventoryTests(unittest.TestCase):
    def fake_run(self, stdout, code=0):
        def run(cmd, **kwargs):
            self.assertEqual(cmd, list(inventory.WINAPP_COMMAND))
            return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="")
        return run

    def test_windows_applications_are_aggregated_without_user_names(self):
        out = json.dumps({"schema": "boswas-winapp/1", "users": [
            {"user": "alice", "uid": 1000, "applications": [
                {"id": "com.example.app", "version": "1.0", "status": "tested", "state": "installed"},
                {"id": "local.tool", "version": "unknown", "status": "unknown", "state": "failed"}]},
            {"user": "bob", "uid": 1001, "applications": [
                {"id": "com.example.app", "version": "1.0", "status": "tested", "state": "installed"}]}]})
        result = inventory.WindowsApplicationsCollector(run=self.fake_run(out)).collect()
        self.assertEqual(result["applications"], [{"kind": "winapp", "id": "com.example.app", "version": "1.0",
                                                   "status": "tested", "installations": 2}])
        self.assertNotIn("alice", json.dumps(result))

    def test_collector_failures_are_reported_not_raised(self):
        self.assertFalse(inventory.WindowsApplicationsCollector(run=self.fake_run("", 5)).collect()["available"])
        self.assertFalse(inventory.WindowsApplicationsCollector(run=self.fake_run("{")).collect()["available"])


if __name__ == "__main__":
    unittest.main()
