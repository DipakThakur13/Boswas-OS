"""boswas CLI with the device agent (Milestone 2): identity, enrollment and
policy come from the agent's public files; device.conf stays the fallback.
Run: python3 -m unittest discover -s tests
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_boswas_cli import CliTestCase  # noqa: E402

from boswas_cli import checks  # noqa: E402

DEVICE_ID = "1b4e28ba-2fa1-41d2-883f-0016d3cca427"
STATUS = {"schema": "boswas-agent-status/1", "device_id": DEVICE_ID, "state": "OFFLINE", "connection": "OFFLINE",
          "agent_version": "1.0~alpha3", "enrollment": "enrolled", "control_plane": "https://cp.example:8443",
          "policy": {"managed": True, "version": "default-4"}, "last_contact": "2026-10-04T10:00:00Z",
          "updated_at": "2026-10-04T12:00:00Z", "maintenance": False}


class AgentIntegrationTests(CliTestCase):
    def test_device_status_uses_the_agent_identity_and_state(self):
        self.fs.write("/etc/boswas/device.conf", 'DEVICE_ID=""\nENROLLMENT_STATE="unenrolled"\nDEVICE_PROFILE="eng"\n')
        self.fs.write("/var/lib/boswas/agent/identity.json", json.dumps({"device_id": DEVICE_ID}))
        self.fs.write("/var/lib/boswas/agent/status.json", json.dumps(STATUS))
        code, out, _ = self.run_cli("--json", "device", "status")
        doc = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual((doc["device"]["device_id"], doc["device"]["enrollment_state"], doc["device"]["policy_version"],
                          doc["device"]["device_profile"]), (DEVICE_ID, "enrolled", "default-4", "eng"))
        self.assertEqual(doc["agent"]["connection"], "OFFLINE")
        code, out, _ = self.run_cli("device", "status")
        self.assertIn("Device state:", out)
        self.assertIn("OFFLINE", out)

    def test_damaged_agent_files_fall_back_to_device_conf(self):
        self.fs.write("/etc/boswas/device.conf", 'ENROLLMENT_STATE="unenrolled"\n')
        self.fs.write("/var/lib/boswas/agent/identity.json", '{"device_id": "my-laptop"}')
        self.fs.write("/var/lib/boswas/agent/status.json", "{broken")
        code, out, _ = self.run_cli("--json", "device", "status")
        doc = json.loads(out)
        self.assertEqual((doc["device"]["device_id"], doc["device"]["enrollment_state"], doc["agent"]),
                         (None, "unenrolled", None))

    def test_management_checks_report_the_agent(self):
        self.assertIn("not installed", checks.agent().detail)
        self.fs.write("/usr/lib/systemd/system/boswas-device-agent.service", "[Unit]\n")
        self.fs.write("/var/lib/boswas/agent/status.json", json.dumps(STATUS))
        result = checks.agent()
        self.assertEqual((result.status, result.scored), (checks.INFO, False))
        self.assertIn("device OFFLINE, Control Plane OFFLINE", result.detail)
        self.assertEqual(checks.enrollment().detail, "enrolled")
