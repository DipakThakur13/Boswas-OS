"""Device agent foundations: identity, configuration, state, typed commands,
outbox and back-off, ledger, signed policies, the managed WinCompat layer,
privacy and inventory.

Run: python3 -m unittest discover -s tests   (needs the openssl command)
"""

import base64
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "boswas-compat"))
REPO = HERE.parents[3]

from boswas_compat import UNSUPPORTED_32BIT_MESSAGE  # noqa: E402
from boswas_compat import policy as compat_policy  # noqa: E402

from boswas_agent import commands, config, identity, inventory, ledger, models, outbox, policydoc, privacy  # noqa: E402
from boswas_agent import state as agent_state  # noqa: E402
from boswas_agent.credentials import FileCredentialStore  # noqa: E402
from boswas_agent.errors import (CommandExpired, IdentityError, InvalidCommand, PolicyVerificationError,  # noqa: E402
                                 PrivacyViolation, UnsupportedCommand)
from boswas_agent.paths import AgentPaths  # noqa: E402
from boswas_agent.policy_store import ManagedCatalog, PolicyStore  # noqa: E402

UUID = "1b4e28ba-2fa1-41d2-883f-0016d3cca427"
DEVICE = "6a2f41a3-c54c-4fc6-9cbb-07f8d7f6a8a1"
NOW = commands.parse_time("2026-10-04T12:00:00Z")


def manifest(**overrides):
    doc = {"id": "com.example.app", "name": "Example", "version": "2.0", "runtime": {"type": "wine", "wineVersion": "10"},
           "architecture": "x86_64", "launch": "C:\\Program Files\\Example\\example.exe", "status": "approved",
           "installer": {"type": "exe", "sha256": "a" * 64}}
    doc.update(overrides)
    return doc


def command(ctype="REFRESH_INVENTORY", payload=None, **overrides):
    doc = {"schema": "boswas-device-command/2", "command_id": UUID, "device_id": DEVICE, "type": ctype,
           "payload": {} if payload is None else payload, "created_at": "2026-10-04T11:00:00Z",
           "expires_at": "2026-10-05T11:00:00Z", "created_by": "operator:7"}
    doc.update(overrides)
    return doc


def install_payload(**overrides):
    payload = {"application_id": "com.example.app", "version": "2.0",
               "installer": {"sha256": "a" * 64, "size": 1234, "file_name": "setup.exe"}, "manifest": manifest()}
    payload.update(overrides)
    return payload


class TempRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.paths = AgentPaths(self.root)
        (self.root / "etc/boswas").mkdir(parents=True)


# --- identity -----------------------------------------------------------------------------

class IdentityTests(TempRoot):
    def test_identity_is_created_once_and_survives_restarts(self):
        first = identity.IdentityStore(self.paths).ensure()
        self.assertEqual((first.source, first.ephemeral), ("generated", False))
        for _ in range(3):                              # "reboots": new processes, same files
            self.assertEqual(identity.IdentityStore(self.paths).ensure(), first)
        self.assertEqual(stat.S_IMODE(self.paths.identity.stat().st_mode), 0o644)
        doc = json.loads(self.paths.identity.read_text())
        self.assertEqual(set(doc), {"schema", "device_id", "created_at", "source", "ephemeral"})

    def test_damaged_identity_is_never_silently_replaced(self):
        identity.IdentityStore(self.paths).ensure()
        self.paths.identity.write_text("{not json")
        with self.assertRaises(IdentityError):
            identity.IdentityStore(self.paths).ensure()
        self.assertEqual(self.paths.identity.read_text(), "{not json")
        new = identity.IdentityStore(self.paths).reset()          # explicit administrator action
        self.assertTrue(models_uuid(new.device_id))

    def test_provisioned_identity_survives_reinstallation(self):
        self.paths.device_conf.write_text(f'DEVICE_ID="{UUID}"\n')
        ident = identity.IdentityStore(self.paths).ensure()
        self.assertEqual((ident.device_id, ident.source), (UUID, "provisioned"))
        other = AgentPaths(self.root / "other")
        (other.root / "etc/boswas").mkdir(parents=True)
        other.device_conf.write_text('DEVICE_ID="52:54:00:12:34:56"\n')
        with self.assertRaises(IdentityError):
            identity.IdentityStore(other).ensure()

    def test_live_session_identity_is_ephemeral(self):
        (self.root / "run/live").mkdir(parents=True)
        self.assertTrue(identity.IdentityStore(self.paths).ensure().ephemeral)

    def test_concurrent_creation_yields_one_identity(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(identity.IdentityStore(self.paths).ensure()))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 8)                   # no thread failed
        self.assertEqual(len({r.device_id for r in results}), 1)

    def test_identity_never_reads_hardware_identifiers(self):
        opened = []
        real_open = os.open

        def spy(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)
        with mock.patch("os.open", side_effect=spy):
            identity.IdentityStore(self.paths).ensure()
        for path in opened:
            for forbidden in ("/sys/class/net", "machine-id", "product_serial", "product_uuid", "board_serial",
                              "/dev/disk"):
                self.assertNotIn(forbidden, path)


def models_uuid(value):
    from boswas_agent.device_identity import is_valid_device_id
    return is_valid_device_id(value)


# --- configuration -----------------------------------------------------------------------

class ConfigTests(TempRoot):
    def test_shipped_device_conf_is_valid(self):
        cfg = config.AgentConfig.from_text((REPO / "config/boswas/device.conf").read_text())
        self.assertEqual((cfg.problems, cfg.warnings), ([], []))
        self.assertFalse(cfg.managed)
        self.assertTrue(cfg.agent_enabled)
        for setting in config.SETTINGS:
            self.assertIn(f"{setting.key}=", (REPO / "config/boswas/device.conf").read_text(), setting.key)

    def test_invalid_values_are_reported_and_replaced_by_defaults(self):
        cfg = config.AgentConfig.from_text('AGENT_ENABLED="maybe"\nHEARTBEAT_INTERVAL="5"\nINVENTORY_POLICY="all"\n'
                                           'CONTROL_PLANE_URL="http://cp.example"\nLOG_LEVEL="trace"\n')
        self.assertEqual(len(cfg.problems), 5)
        self.assertEqual(cfg.values["HEARTBEAT_INTERVAL"], "300")
        self.assertFalse(cfg.valid)

    def test_control_plane_settings(self):
        for url in ("https://user:pw@cp.example", "https://cp.example/?x=1", "https://", "https://cp.example:99999"):
            cfg = config.AgentConfig.from_text(f'CONTROL_PLANE_URL="{url}"\nCONTROL_PLANE_CA="/etc/boswas/ca.pem"\n')
            self.assertTrue(cfg.problems, url)
        cfg = config.AgentConfig.from_text('CONTROL_PLANE_URL="https://cp.boswas.invalid:8443/base"\n')
        self.assertIn("CONTROL_PLANE_CA", cfg.problems[0])
        cfg = config.AgentConfig.from_text('CONTROL_PLANE_URL="https://10.0.2.2:8443"\n'
                                           'CONTROL_PLANE_CA="/etc/boswas/control-plane-ca.pem"\n')
        self.assertTrue(cfg.valid, cfg.problems)
        self.assertTrue(cfg.managed)
        cfg = config.AgentConfig.from_text('CONTROL_PLANE_URL="https://cp"\nCONTROL_PLANE_CA="/home/u/ca.pem"\n')
        self.assertFalse(cfg.valid)

    def test_secrets_and_shell_syntax_are_refused(self):
        marker = "-----BEGIN " + "PRIVATE" + " KEY-----"
        for text in (f'DEVICE_NAME="{marker}"\n', 'ENROLLMENT_TOKEN="abc"\n', 'API_SECRET="x"\n',
                     'DEVICE_PROFILE="x; rm -rf /"\n', 'DEVICE_NAME="$(id)"\n', 'DEVICE_NAME="a`b`"\n',
                     'DEVICE_NAME="unbalanced\n'):
            self.assertFalse(config.AgentConfig.from_text(text).valid, text)
        self.assertEqual(config.AgentConfig.from_text('SOMETHING="x"\n').warnings, ["SOMETHING: unknown key (ignored)"])

    def test_file_permissions_and_symlinks(self):
        conf = self.paths.device_conf
        conf.write_text('AGENT_ENABLED="yes"\n')
        conf.chmod(0o666)
        cfg = config.AgentConfig.load(conf, check_owner=os.getuid())
        self.assertIn("insecure ownership or permissions", cfg.problems[0])
        conf.chmod(0o644)
        self.assertTrue(config.AgentConfig.load(conf, check_owner=os.getuid()).valid)
        link = self.root / "etc/boswas/link.conf"
        link.symlink_to(conf)
        self.assertFalse(config.AgentConfig.load(link).valid)
        self.assertTrue(config.AgentConfig.load(self.root / "missing.conf").valid)       # defaults: standalone


# --- state -------------------------------------------------------------------------------------

class StateTests(unittest.TestCase):
    def test_precedence(self):
        F, S = agent_state.Facts, models.DeviceState
        self.assertEqual(agent_state.evaluate(F())[0], S.READY)
        self.assertEqual(agent_state.evaluate(F(runtime_healthy=False))[0], S.DEGRADED)
        self.assertEqual(agent_state.evaluate(F(runtime_healthy=False, offline=True))[0], S.OFFLINE)
        self.assertEqual(agent_state.evaluate(F(offline=True, updating=True))[0], S.UPDATING)
        self.assertEqual(agent_state.evaluate(F(updating=True, maintenance=True))[0], S.MAINTENANCE)
        self.assertEqual(agent_state.evaluate(F(maintenance=True, config_ok=False))[0], S.ERROR)
        self.assertEqual(agent_state.evaluate(F(identity_ok=False, problems=["damaged"]))[1], ["damaged"])

    def test_transitions(self):
        S = models.DeviceState
        m = agent_state.StateMachine(clock=lambda: 0)
        m.transition(S.READY)
        m.transition(S.OFFLINE)
        m.transition(S.ERROR)
        with self.assertRaises(agent_state.InvalidTransition):
            m.transition(S.READY)                       # recovery goes through INITIALIZING
        self.assertTrue(m.move_to(S.READY, ["recovered"]))
        self.assertEqual([t.new for t in m.history][-2:], [S.INITIALIZING, S.READY])
        self.assertFalse(m.move_to(S.READY, ["still ready"]))
        for old, targets in agent_state.TRANSITIONS.items():
            for new in S:
                machine = agent_state.StateMachine()
                machine.state = old
                machine.move_to(new, [])                # any target is reachable by an allowed path
                self.assertEqual(machine.state, new)


# --- typed commands -------------------------------------------------------------------------

class CommandTests(unittest.TestCase):
    def parse(self, doc, **kw):
        return commands.parse_command(doc, now=NOW, **kw)

    def test_every_type_with_a_valid_payload(self):
        payloads = {
            "INSTALL_APPLICATION": install_payload(), "UPDATE_APPLICATION": install_payload(),
            "REMOVE_APPLICATION": {"application_id": "com.example.app"},
            "LAUNCH_APPLICATION": {"application_id": "com.example.app"},
            "STOP_APPLICATION": {"application_id": "com.example.app"},
            "REPAIR_APPLICATION": {"application_id": "com.example.app"},
            "REFRESH_INVENTORY": {}, "APPLY_POLICY": {"policy_version": "default-3"},
            "UPDATE_AGENT": {"version": "1.0~alpha4"}}
        self.assertEqual(set(payloads), {t.value for t in commands.CommandType})
        for ctype, payload in payloads.items():
            cmd = self.parse(command(ctype, payload))
            self.assertEqual(cmd.type.value, ctype)
            self.assertEqual(self.parse(cmd.to_dict()), cmd)          # serialisation round trip

    def test_unknown_and_shell_like_commands_are_rejected(self):
        for ctype in ("EXECUTE_SHELL_COMMAND", "RUN", "WIPE_DEVICE", "", None, 7):
            with self.assertRaises(UnsupportedCommand):
                self.parse(command(ctype))
        for payload in ({"command": "rm -rf /"}, {"script": "x"}, {"application_id": "com.example.app", "args": ["x"]}):
            with self.assertRaises(InvalidCommand):
                self.parse(command("LAUNCH_APPLICATION", payload))

    def test_malformed_envelopes(self):
        bad = [command(schema="x"), command(command_id="1"), command(device_id="dev"), command(created_by="root"),
               command(created_at="yesterday"), command(expires_at="2026-10-04T10:00:00Z"),
               command(expires_at="2026-10-20T11:00:00Z"), {**command(), "extra": 1}, ["not", "a", "dict"]]
        for doc in bad:
            with self.assertRaises(InvalidCommand, msg=str(doc)[:80]):
                self.parse(doc)
        with self.assertRaises(InvalidCommand):
            self.parse(command(), device_id="2b4e28ba-2fa1-41d2-883f-0016d3cca427")    # addressed to another device

    def test_expired_commands_are_never_returned(self):
        with self.assertRaises(CommandExpired):
            commands.parse_command(command(), now=commands.parse_time("2026-10-05T11:00:00Z"))

    def test_install_payload_rules(self):
        bad = [
            install_payload(application_id="Com.Bad"),
            install_payload(installer={"sha256": "a" * 64, "size": 1234, "file_name": "../../etc/passwd"}),
            install_payload(installer={"sha256": "a" * 64, "size": 0, "file_name": "x.exe"}),
            install_payload(installer={"sha256": "A" * 64, "size": 1, "file_name": "x.exe"}),
            install_payload(manifest=manifest(id="com.other.app")),
            install_payload(manifest=manifest(version="9")),
            install_payload(manifest=manifest(installer={"type": "exe", "sha256": "b" * 64})),
            install_payload(manifest=manifest(status="blocked")),
            install_payload(manifest=manifest(environment={"LD_PRELOAD": "/tmp/x.so"})),
            install_payload(manifest={"id": "com.example.app"}),
        ]
        for payload in bad:
            with self.assertRaises(InvalidCommand):
                self.parse(command("INSTALL_APPLICATION", payload))

    def test_32_bit_catalog_entries_are_refused(self):
        payload = install_payload(manifest=manifest(architecture="x86"))
        cmd = self.parse(command("INSTALL_APPLICATION", payload))
        self.assertEqual(commands.architecture_refusal(cmd.type, cmd.payload), UNSUPPORTED_32BIT_MESSAGE)
        self.assertIsNone(commands.architecture_refusal(cmd.type, install_payload()))

    def test_status_transitions(self):
        S = commands.CommandStatus
        self.assertTrue(commands.can_transition(S.QUEUED, S.SENT))
        self.assertTrue(commands.can_transition(S.QUEUED, S.CANCELLED))
        self.assertFalse(commands.can_transition(S.SENT, S.CANCELLED))
        self.assertFalse(commands.can_transition(S.SUCCEEDED, S.FAILED))
        for terminal in commands.TERMINAL_STATUSES:
            self.assertFalse(any(commands.can_transition(terminal, s) for s in S))

    def test_results_carry_only_allowlisted_facts(self):
        outcome = commands.CommandOutcome.failed("BAD CODE", "x\x1b[2Jy" * 200, application_id="com.example.app",
                                                 stdout="secret output", healthy="yes")
        doc = commands.result_document(UUID, DEVICE, outcome, "2026-10-04T12:00:00Z")
        self.assertEqual(doc["result"], {"application_id": "com.example.app"})
        self.assertEqual(doc["error"]["code"], "FAILED")
        self.assertNotIn("\x1b", doc["error"]["message"])
        self.assertLessEqual(len(doc["error"]["message"]), commands.MAX_ERROR_MESSAGE)
        privacy.check(doc)
        with self.assertRaises(PrivacyViolation):
            privacy.check({**doc, "result": {"stdout": "x"}})


# --- outbox and back-off -----------------------------------------------------------------------

class OutboxTests(TempRoot):
    def test_persistence_coalescing_and_ack(self):
        box = outbox.Outbox(self.paths.outbox)
        box.put("result", {"n": 1})
        box.put("inventory", {"rev": 1})
        box.put("inventory", {"rev": 2})
        box.put("event", {"e": 1})
        again = outbox.Outbox(self.paths.outbox)                  # after a restart
        self.assertEqual([(k, d) for _, k, d in again.items()],
                         [("result", {"n": 1}), ("inventory", {"rev": 2}), ("event", {"e": 1})])
        item_id = again.items()[0][0]
        again.ack(item_id)
        self.assertEqual(len(again), 2)
        self.assertEqual(stat.S_IMODE(self.paths.outbox.stat().st_mode), 0o700)

    def test_bounded_and_events_dropped_first(self):
        box = outbox.Outbox(self.paths.outbox, max_items=4)
        box.put("result", {"n": 1})
        box.put("event", {"e": 1})
        box.put("result", {"n": 2})
        box.put("event", {"e": 2})
        box.put("result", {"n": 3})
        kinds = [k for _, k, _ in box.items()]
        self.assertEqual(kinds.count("result"), 3)
        self.assertEqual(len(kinds), 4)

    def test_damaged_items_are_discarded(self):
        box = outbox.Outbox(self.paths.outbox)
        name = box.put("result", {"n": 1})
        (self.paths.outbox / name).write_text("{broken")
        self.assertEqual(box.items(), [])
        with self.assertRaises(ValueError):
            box.put("shell", {})

    def test_backoff_is_bounded_with_jitter(self):
        b = outbox.Backoff(base=5, cap=900, rng=lambda: 1.0)
        delays = [b.failure() for _ in range(20)]
        self.assertEqual(delays[:4], [5, 10, 20, 40])
        self.assertEqual(max(delays), 900)
        b.success()
        self.assertEqual(b.failure(), 5)
        low = outbox.Backoff(base=5, cap=900, rng=lambda: 0.0)
        self.assertEqual([low.failure() for _ in range(3)], [2.5, 5, 10])      # never zero: no busy loop


class LedgerTests(TempRoot):
    def test_ledger_and_event_log(self):
        led = ledger.CommandLedger(self.paths.ledger)
        led.record("c2", {"status": "ACKNOWLEDGED", "received_at": "2026-10-04T12:00:02Z"})
        led.record("c1", {"status": "ACKNOWLEDGED", "received_at": "2026-10-04T12:00:01Z"})
        led.record("c3", {"status": "SUCCEEDED", "received_at": "2026-10-04T12:00:00Z"})
        self.assertEqual([e["command_id"] for e in led.with_status("ACKNOWLEDGED")], ["c1", "c2"])
        led.record("c1", {"status": "RUNNING"})
        self.assertEqual(led.get("c1")["received_at"], "2026-10-04T12:00:01Z")
        self.assertEqual(stat.S_IMODE(self.paths.ledger.stat().st_mode), 0o600)
        log = ledger.EventLog(self.paths.events)
        log.append("SECURITY_EVENT", "bad\x1b[31m thing\n" + "x" * 1000, application_id="com.example.app")
        event = log.recent()[0]
        self.assertNotIn("\x1b", event["detail"])
        self.assertLessEqual(len(event["detail"]), 300)


# --- signed policy ---------------------------------------------------------------------------

class PolicyTests(TempRoot):
    def setUp(self):
        super().setUp()
        self.key = self.root / "policy.key"
        self.pub = policydoc.generate_signing_key(self.key)
        self.doc = policydoc.default_policy(issued_at="2026-10-04T12:00:00Z")

    def test_sign_and_verify(self):
        env = policydoc.make_envelope(self.doc, self.key, self.pub)
        self.assertEqual(policydoc.open_envelope(env, self.pub), self.doc)
        self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o600)

    def test_tampered_or_foreign_policies_are_rejected(self):
        env = policydoc.make_envelope(self.doc, self.key, self.pub)
        tampered = json.loads(base64.b64decode(env["document"]))
        tampered["compat"]["unlisted_apps"] = "allow"
        bad_doc = {**env, "document": base64.b64encode(policydoc.canonical_json(tampered)).decode()}
        other_pub = policydoc.generate_signing_key(self.root / "other.key")
        other = policydoc.make_envelope(self.doc, self.root / "other.key", other_pub)
        for envelope, why in ((bad_doc, "signature"), ({**other, "key_id": env["key_id"]}, "signature"),
                              (other, "unknown key"), ({**env, "algorithm": "none"}, "algorithm"),
                              ({**env, "signature": "!!"}, "encoding"), ({**env, "extra": 1}, "envelope")):
            with self.assertRaises(PolicyVerificationError, msg=why):
                policydoc.open_envelope(envelope, self.pub)

    def test_invalid_documents_are_rejected_even_when_signed(self):
        for change in ({"compat": {**self.doc["compat"], "require_apparmor": False}},
                       {"agent": {**self.doc["agent"], "allowed_commands": ["EXECUTE_SHELL_COMMAND"]}},
                       {"version": "default-9"}, {"unexpected": True}):
            doc = {**self.doc, **change}
            with self.assertRaises(PolicyVerificationError):
                policydoc.make_envelope(doc, self.key, self.pub)
            data = policydoc.canonical_json(doc)       # signed by the right key, but invalid
            env = {"schema": policydoc.ENVELOPE_SCHEMA, "algorithm": "ed25519", "key_id": policydoc.key_id(self.pub),
                   "document": base64.b64encode(data).decode(),
                   "signature": base64.b64encode(policydoc.sign(data, self.key)).decode()}
            with self.assertRaises(PolicyVerificationError):
                policydoc.open_envelope(env, self.pub)

    def test_rendering_parses_in_boswas_compat(self):
        doc = {**self.doc, "compat": {**self.doc["compat"], "blocked_applications": ["com.bad.app"],
                                      "allowed_applications": []}}
        path = self.root / "policy.conf"
        path.write_text(policydoc.render_compat_policy(doc))
        parsed = compat_policy.Policy.load(path)
        self.assertEqual(parsed.problems, ())
        self.assertEqual(parsed.allowed_applications, frozenset())
        self.assertEqual(parsed.blocked_applications, frozenset({"com.bad.app"}))
        self.assertTrue(parsed.require_apparmor)
        self.assertFalse(parsed.unlisted_apps)


class PolicyStoreTests(TempRoot):
    def setUp(self):
        super().setUp()
        self.key = self.root / "policy.key"
        self.pub = policydoc.generate_signing_key(self.key)
        self.creds = FileCredentialStore(self.paths.credentials)
        self.creds.pin_policy_key(self.pub)
        self.store = PolicyStore(self.paths, self.creds)
        env = mock.patch.dict(os.environ, {"BOSWAS_SYSROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def envelope(self, sequence, issued="2026-10-04T12:00:00Z", **compat):
        doc = policydoc.default_policy(sequence=sequence, issued_at=issued)
        doc["compat"].update(compat)
        return policydoc.make_envelope(doc, self.key, self.pub)

    def test_apply_and_compat_uses_the_managed_policy(self):
        doc, changed = self.store.apply(self.envelope(1, unlisted_apps="allow"))
        self.assertTrue(changed)
        policy = compat_policy.Policy.load()
        self.assertTrue(policy.managed)
        self.assertTrue(policy.unlisted_apps)
        self.assertEqual(self.store.version(), "default-1")
        self.assertFalse(self.store.apply(self.envelope(1, unlisted_apps="allow"))[1])     # same: no change

    def test_rollback_and_unverified_policies_keep_the_current_one(self):
        self.store.apply(self.envelope(5, issued="2026-10-04T12:00:00Z"))
        before = self.paths.managed_policy.read_text()
        with self.assertRaises(PolicyVerificationError):
            self.store.apply(self.envelope(4, issued="2026-10-04T13:00:00Z"))
        with self.assertRaises(PolicyVerificationError):
            self.store.apply({"schema": "boswas-signed-policy/1"})
        self.assertEqual(self.paths.managed_policy.read_text(), before)
        self.assertEqual(self.store.version(), "default-5")

    def test_no_pinned_key_means_no_policy(self):
        self.creds.clear()
        with self.assertRaises(PolicyVerificationError):
            self.store.apply(self.envelope(1))
        self.assertFalse(self.paths.managed_policy.exists())

    def test_clear_restores_the_local_policy(self):
        self.store.apply(self.envelope(1))
        catalog = ManagedCatalog(self.paths)
        catalog.install(manifest())
        self.store.clear()
        catalog.clear()
        self.assertFalse(self.paths.managed_policy.exists())
        self.assertEqual(catalog.ids(), [])

    def test_managed_catalog_validates_manifests(self):
        catalog = ManagedCatalog(self.paths)
        path = catalog.install(manifest())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        from boswas_compat.errors import ManifestError
        with self.assertRaises(ManifestError):
            catalog.install(manifest(environment={"LD_PRELOAD": "x"}))
        self.assertEqual(catalog.ids(), ["com.example.app"])


# --- privacy and inventory ---------------------------------------------------------------------

def fake_run(responses):
    import subprocess

    def run(argv, **kwargs):
        for prefix, (code, out) in responses.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
        return subprocess.CompletedProcess(argv, 127, stdout="", stderr="")
    return run


WINAPP_USERS = {"users": [
    {"user": "alice", "uid": 1000, "running": ["com.example.app"], "applications": [
        {"id": "com.example.app", "version": "2.0", "status": "approved", "state": "installed",
         "architecture": "x86_64", "allowed": True, "compatibility": "SUPPORTED"},
        {"id": "local.tool", "version": "unknown", "status": "unknown", "state": "failed", "architecture": "x86_64",
         "allowed": True, "compatibility": "SUPPORTED"}]},
    {"user": "bob", "uid": 1001, "running": [], "applications": [
        {"id": "com.example.app", "version": "2.0", "status": "approved", "state": "installed",
         "architecture": "x86_64", "allowed": True, "compatibility": "SUPPORTED"},
        {"id": "com.old.app", "version": "1", "status": "tested", "state": "installed", "architecture": "x86",
         "allowed": False, "compatibility": "UNSUPPORTED_ARCHITECTURE"}]}]}
RUNTIME = {"wine": {"available": True, "version": "wine-10.0"}, "architectures": ["x86_64"],
           "bubblewrap": {"version": "0.11"}, "apparmor": {"mode": "enforce"}, "policy": {"managed": False},
           "healthy": True}


class InventoryTests(TempRoot):
    def inventory(self, runtime=None):
        r = self.root
        for rel, text in (("usr/lib/boswas/release", 'BOSWAS_NAME="Boswas OS"\nBOSWAS_VERSION="v1 Alpha"\n'
                                                     'BOSWAS_VERSION_ID="1.0~alpha3"\n'),
                          ("usr/lib/boswas/image-info", 'BOSWAS_BUILD_ID="BOS-1"\n'), ("etc/debian_version", "13.7\n"),
                          ("proc/cpuinfo", "model name\t: Test CPU\n"), ("proc/meminfo", "MemTotal: 8388608 kB\n"),
                          ("sys/class/dmi/id/sys_vendor", "QEMU\n"), ("sys/class/dmi/id/product_name", "Standard PC\n"),
                          ("sys/class/dmi/id/product_serial", "SERIAL-SECRET-123\n"),
                          ("sys/class/dmi/id/product_uuid", "4c4c4544-0000-0000-0000-000000000000\n"),
                          ("sys/class/net/eth0/address", "52:54:00:aa:bb:cc\n"), ("etc/machine-id", "f" * 32 + "\n")):
            (r / rel).parent.mkdir(parents=True, exist_ok=True)
            (r / rel).write_text(text)
        run = fake_run({("/usr/bin/boswas-winapp", "--json", "list", "--all-users"): (0, json.dumps(WINAPP_USERS)),
                        ("/usr/bin/boswas-winapp", "--json", "runtime"): runtime or (0, json.dumps(RUNTIME)),
                        ("dpkg", "--print-architecture"): (0, "amd64\n"),
                        ("dpkg-query",): (0, "boswas-compat\tinstalled\t1.0~alpha3\nwine64\tinstalled\t10.0~repack-6\n"
                                             "evil\tinstalled\t1\nboswas-cli\tnot-installed\t\n")})
        return inventory.SystemInventory(self.paths, run=run)

    def full(self, doc):
        return {"schema": inventory.INVENTORY_SCHEMA, "device_id": DEVICE, "revision": 1,
                "collected_at": "2026-10-04T12:00:00Z", **doc}

    def test_inventory_is_allowlisted_and_privacy_checked(self):
        doc = self.full(self.inventory().collect("standard"))
        privacy.check(doc)
        text = json.dumps(doc)
        for secret in ("SERIAL-SECRET-123", "4c4c4544", "52:54:00:aa:bb:cc", "f" * 32, "alice", "bob"):
            self.assertNotIn(secret, text)
        self.assertEqual([p["name"] for p in doc["packages"]], ["boswas-compat", "wine64"])
        self.assertEqual(doc["os"]["architecture"], "amd64")
        self.assertEqual(doc["hardware"]["memory_gib"], 8.0)
        apps = {a["id"]: a for a in doc["windows_applications"]}
        self.assertEqual((apps["com.example.app"]["installations"], apps["com.example.app"]["running"],
                          apps["com.example.app"]["app_state"]), (2, 1, "RUNNING"))
        self.assertEqual(apps["local.tool"]["app_state"], "ERROR")
        self.assertEqual(apps["com.old.app"]["app_state"], "UNSUPPORTED")
        self.assertEqual(doc["compatibility"]["architectures"], ["x86_64"])

    def test_an_unhealthy_runtime_is_reported_as_such(self):
        # boswas-winapp runtime exits 1 when unhealthy (here: the AppArmor profile is not loaded).
        unhealthy = {**RUNTIME, "apparmor": {"mode": "unloaded"}, "healthy": False}
        compat = self.inventory(runtime=(1, json.dumps(unhealthy))).compatibility()
        self.assertEqual((compat["available"], compat["healthy"], compat["architectures"], compat["apparmor"]),
                         (True, False, ["x86_64"], "unloaded"))
        broken = self.inventory(runtime=(2, "boswas-winapp: error")).compatibility()
        self.assertEqual((broken["available"], broken["architectures"]), (False, []))

    def test_minimal_policy_and_extra_fields(self):
        doc = self.full(self.inventory().collect("minimal"))
        self.assertNotIn("hardware", doc)
        privacy.check(doc)
        for extra in ({"hardware": {"serial": "x"}}, {"browser_history": []}, {"users": ["alice"]}):
            with self.assertRaises(PrivacyViolation):
                privacy.check({**doc, **extra})
        with self.assertRaises(ValueError):
            self.inventory().collect("off")

    def test_revision_digest_ignores_collection_time(self):
        a = self.full(self.inventory().collect("standard"))
        b = {**a, "collected_at": "2030-01-01T00:00:00Z", "revision": 9}
        self.assertEqual(inventory.content_digest(a), inventory.content_digest(b))


class SchemaSyncTests(unittest.TestCase):
    def schema(self, name):
        return json.loads((HERE.parents[1] / "schemas" / f"{name}.schema.json").read_text())

    def test_enums_match_the_models(self):
        cmd = self.schema("device-command-v2")
        self.assertEqual(cmd["properties"]["type"]["enum"], [c.value for c in commands.CommandType])
        hb = self.schema("heartbeat-v2")
        self.assertEqual(hb["properties"]["state"]["enum"], [s.value for s in models.DeviceState])
        inv = self.schema("inventory-v1")
        self.assertEqual(inv["properties"]["windows_applications"]["items"]["properties"]["app_state"]["enum"],
                         [s.value for s in models.AppState])
        self.assertEqual(inv["properties"]["packages"]["items"]["properties"]["name"]["enum"],
                         list(inventory.TRACKED_PACKAGES))
        ev = self.schema("device-event-v1")
        self.assertEqual(ev["properties"]["events"]["items"]["properties"]["type"]["enum"],
                         [t.value for t in models.DEVICE_EVENT_TYPES])
        res = self.schema("command-result-v1")
        self.assertEqual(set(res["properties"]["result"]["properties"]), set(commands.RESULT_FIELDS))
        pol = self.schema("policy-v1")
        self.assertEqual(pol["properties"]["agent"]["properties"]["allowed_commands"]["items"]["enum"],
                         [c.value for c in commands.CommandType])

    def test_app_states_match_boswas_compat(self):
        from boswas_compat import ops
        self.assertEqual(tuple(s.value for s in models.AppState), ops.APP_STATES)

    def test_privacy_allowlists_match_schemas(self):
        pairs = {"boswas-heartbeat/2": "heartbeat-v2", "boswas-status-report/1": "status-report-v1",
                 "boswas-inventory/1": "inventory-v1", "boswas-command-result/1": "command-result-v1",
                 "boswas-device-event/1": "device-event-v1", "boswas-enrollment-request/1": "enrollment-request-v1",
                 "boswas-compliance-report/1": "compliance-report-v1"}
        self.assertEqual(set(pairs), set(privacy.ALLOWLISTS))
        for message, name in pairs.items():
            self.assertEqual(set(privacy.ALLOWLISTS[message]), set(self.schema(name)["properties"]), message)


if __name__ == "__main__":
    unittest.main()
