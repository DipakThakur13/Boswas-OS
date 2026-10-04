"""Unit tests for boswas-winapp. Run: python3 -m unittest discover -s tests

Nothing here runs Wine or bubblewrap: system files come from a temporary fake
root (BOSWAS_SYSROOT), the user's home is a temporary directory, and a fake
executor stands in for the sandbox. The real sandbox, Wine and AppArmor are
exercised by tests/compatibility/ (image) and tests/boot/ (VM).
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
REPO = HERE.parents[3]

from boswas_compat import cli, executor, installer, manifest, ops, paths, policy, sandbox, store  # noqa: E402
from boswas_compat.catalog import Catalog  # noqa: E402
from boswas_compat.errors import (Busy, ManifestError, NotFound, Refused, Unavailable,  # noqa: E402
                                  UsageError, WinAppError)

COMPAT = REPO / "compatibility"
ENFORCED = "boswas-winapp (enforce)"
REAL_REQUIRE_USER = ops.require_user


# --- fixtures ------------------------------------------------------------------------------

def pe_bytes(machine=0x8664, subsystem=3, extra=b"") -> bytes:
    """A minimal PE header (enough for inspection; not a runnable program)."""
    data = bytearray(0x200)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, machine)
    struct.pack_into("<H", data, 0x80 + 24, 0x20B if machine == 0x8664 else 0x10B)
    struct.pack_into("<H", data, 0x80 + 24 + 68, subsystem)
    return bytes(data) + extra


def manifest_doc(**overrides) -> dict:
    doc = {
        "id": "com.example.app", "name": "Example App", "version": "1.0",
        "runtime": {"type": "wine", "wineVersion": "10"}, "architecture": "x86_64",
        "launch": "C:\\Program Files\\Example\\example.exe", "status": "experimental",
    }
    doc.update(overrides)
    return doc


class FakeExecutor:
    """Simulates the sandbox: wineboot creates a prefix, installers create
    programs, launched programs print a line and exit."""

    def __init__(self):
        self.calls = []
        self.install_creates = ["Program Files/Fake App/fake.exe", "Program Files/Fake App/unins000.exe"]
        self.exit_code = 0
        self.confinement = ENFORCED
        self.refused = None

    @staticmethod
    def host_sandbox(argv):
        i = argv.index("--bind")
        while not argv[i + 2].startswith(paths.VIEW_ROOT):
            i = argv.index("--bind", i + 1)
        return Path(argv[i + 1])

    def run(self, argv, log_path, *, mirror=None, timeout=None, title=""):
        command = argv[argv.index("--") + 2:]
        self.calls.append({"argv": argv, "command": command, "title": title})
        sb = self.host_sandbox(argv)
        drive_c = sb / "prefix" / "drive_c"
        code = 0
        if "wineboot" in command:
            (drive_c / "windows" / "system32").mkdir(parents=True, exist_ok=True)
            (drive_c / "windows" / "system32" / "notepad.exe").write_bytes(pe_bytes())
            (sb / "prefix" / "system.reg").write_text("WINE REGISTRY Version 2\n")
        elif "regedit" in command:
            pass
        elif title == "installer":
            for rel in self.install_creates:
                target = drive_c / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(pe_bytes())
            code = self.exit_code
        else:
            code = self.exit_code
            if mirror is not None:
                mirror.write("hello from windows\n")
        with open(log_path, "a") as fh:
            fh.write(f"boswas-winapp-exec: confinement={self.confinement}\n\x1b[31mred\x1b[0m output\n")
        return executor.RunResult(exit_code=77 if self.refused else code, confinement=self.confinement,
                                  refused=self.refused, log=str(log_path))


class CompatTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "root"
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"BOSWAS_SYSROOT": str(self.root), "BOSWAS_WINAPP_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        # Shipped data, as installed by debian/rules.
        share = self.root / "usr/share/boswas/compat"
        share.mkdir(parents=True)
        shutil.copy(COMPAT / "wine/runtime.conf", share / "runtime.conf")
        shutil.copy(COMPAT / "installers/types.json", share / "installers.json")
        shutil.copy(COMPAT / "prefixes/defaults.reg", share / "prefix-defaults.reg")
        (share / "manifests").mkdir()
        (self.root / "etc/boswas/compat/manifests").mkdir(parents=True)
        self.write_policy((COMPAT / "policies/policy.conf").read_text())
        for exe in ("usr/lib/wine/wine64", "usr/lib/wine/wineserver64", "usr/bin/bwrap",
                    "usr/lib/boswas/compat/winapp-exec"):
            p = self.root / exe
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("#!/bin/sh\necho 'wine-10.0 (Debian 10.0~repack-6)'\n")
            p.chmod(0o755)
        self.executor = FakeExecutor()
        user = mock.patch.object(ops, "require_user", lambda: None)
        user.start()
        self.addCleanup(user.stop)

    def write_policy(self, text):
        p = self.root / "etc/boswas/compat/policy.conf"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def set_policy(self, **values):
        text = (COMPAT / "policies/policy.conf").read_text()
        for key, value in values.items():
            text += f'{key}="{value}"\n'
        self.write_policy(text)

    def add_manifest(self, layer="local", **overrides):
        doc = manifest_doc(**overrides)
        directory = {"local": self.root / "etc/boswas/compat/manifests",
                     "system": self.root / "usr/share/boswas/compat/manifests"}[layer]
        (directory / f"{doc['id']}.json").write_text(json.dumps(doc))
        return doc

    def ctx(self, out=None):
        return ops.Context(home=self.home, store=store.AppStore(self.home), catalog=Catalog(),
                           policy=policy.Policy.load(), runtime=ops.load_runtime(),
                           session=sandbox.HostSession(uid=os.getuid(), username="tester", home=self.home),
                           executor=self.executor, out=out)

    def installer_file(self, name="setup.exe", content=None):
        p = self.tmp / name
        p.write_bytes(content if content is not None else pe_bytes(extra=b"Nullsoft Install System v3"))
        return p


# --- manifests -----------------------------------------------------------------------------

class ManifestTests(CompatTestCase):
    def test_example_and_user_format_are_valid(self):
        doc = json.loads((COMPAT / "manifests/examples/example.application.json").read_text())
        self.assertEqual(manifest.validate(doc), [])
        # The minimal format from the platform specification.
        spec = {"id": "example.application", "name": "Example Application", "version": "1.0",
                "runtime": {"type": "wine", "wineVersion": "10"}, "architecture": "x86_64",
                "dependencies": [], "environment": {}, "winetricks": [], "launch": "example.exe",
                "status": "untested"}
        self.assertEqual(manifest.validate(spec), [])

    def test_required_keys_and_unknown_keys(self):
        problems = manifest.validate({"id": "a.b", "surprise": 1})
        self.assertTrue(any("unknown key 'surprise'" in p for p in problems))
        for key in manifest.REQUIRED_KEYS[1:]:
            self.assertTrue(any(key in p for p in problems), key)

    def test_invalid_ids(self):
        for bad in ("App.Example", "noid", "../etc", "a..b", "-a.b", "a.b-", "a/b.c", "x." + "a" * 100):
            self.assertFalse(manifest.valid_id(bad), bad)
        for good in ("example.application", "com.example.app-2", "local.my-tool"):
            self.assertTrue(manifest.valid_id(good), good)

    def test_tested_and_approved_must_pin_installer(self):
        for status in ("tested", "approved"):
            problems = manifest.validate(manifest_doc(status=status))
            self.assertTrue(any("installer.sha256" in p for p in problems), status)
            self.assertEqual(manifest.validate(manifest_doc(status=status, installer={"sha256": "a" * 64})), [])

    def test_sandbox_controlled_environment_is_rejected(self):
        for key in ("LD_PRELOAD", "PATH", "HOME", "XDG_CONFIG_HOME", "WINEPREFIX", "DBUS_SESSION_BUS_ADDRESS",
                    "BOSWAS_X", "lowercase"):
            problems = manifest.validate(manifest_doc(environment={key: "x"}))
            self.assertTrue(problems, key)
        self.assertTrue(manifest.validate(manifest_doc(environment={"WINEDLLOVERRIDES": "winemenubuilder.exe=n"})))
        self.assertEqual(manifest.validate(manifest_doc(environment={"WINEDLLOVERRIDES": "d3d11=n", "MY_APP_MODE": "1"})), [])

    def test_launch_paths(self):
        for bad in ("D:\\app.exe", "\\\\server\\share\\app.exe", "..\\app.exe", "C:\\a\\..\\b.exe", "notes.txt", ""):
            self.assertIsNotNone(manifest.windows_path_problem(bad), bad)
        for good in ("C:\\Program Files\\X\\x.exe", "Program Files/X/x.exe", "x.exe", "c:/tools/run.bat"):
            self.assertIsNone(manifest.windows_path_problem(good), good)

    def test_schema_file_matches_implementation(self):
        schema = json.loads((COMPAT / "manifests/schema/manifest-v1.schema.json").read_text())
        props = schema["properties"]
        self.assertEqual(set(props), manifest.TOP_LEVEL_KEYS)
        self.assertEqual(tuple(schema["required"]), manifest.REQUIRED_KEYS)
        self.assertEqual(tuple(props["status"]["enum"]), manifest.STATUSES)
        self.assertEqual(tuple(props["architecture"]["enum"]), manifest.ARCHITECTURES)
        self.assertEqual(tuple(props["installer"]["properties"]["type"]["enum"]), manifest.INSTALLER_TYPES)
        self.assertEqual(tuple(props["sandbox"]["properties"]["folders"]["items"]["enum"]), manifest.FOLDERS)
        self.assertEqual(props["id"]["pattern"].replace("(", "(?:").replace("(?:?:", "(?:"),
                         manifest.ID_RE.pattern)
        pinned = schema["allOf"][0]["if"]["properties"]["status"]["enum"]
        self.assertEqual(tuple(pinned), manifest.PINNED_STATUSES)

    def test_load_checks_file_name_and_json(self):
        d = self.tmp / "m"
        d.mkdir()
        (d / "wrong.json").write_text(json.dumps(manifest_doc()))
        with self.assertRaises(ManifestError):
            manifest.load(d / "wrong.json")
        (d / "com.example.app.json").write_text("{not json")
        with self.assertRaises(ManifestError):
            manifest.load(d / "com.example.app.json")
        (d / "com.example.app.json").write_text(json.dumps(manifest_doc()))
        m = manifest.load(d / "com.example.app.json", layer="local")
        self.assertEqual((m.id, m.layer, m.sandbox.network, m.sandbox.display), ("com.example.app", "local", False, True))


# --- catalog and policy -------------------------------------------------------------------------

class CatalogPolicyTests(CompatTestCase):
    def test_local_overrides_system(self):
        self.add_manifest("system", version="1.0")
        self.add_manifest("local", version="2.0")
        m = Catalog().get("com.example.app")
        self.assertEqual((m.version, m.layer), ("2.0", "local"))

    def test_blocked_in_any_layer_wins(self):
        self.add_manifest("system", status="blocked")
        self.add_manifest("local", status="experimental")
        self.assertEqual(Catalog().get("com.example.app").status, "blocked")

    def test_invalid_manifests_are_skipped_and_reported(self):
        (self.root / "etc/boswas/compat/manifests/bad.app.json").write_text('{"id": "bad.app"}')
        catalog = Catalog()
        self.assertIsNone(catalog.get("bad.app"))
        self.assertTrue(catalog.problems)

    def test_lookup_by_installer_hash(self):
        self.add_manifest(installer={"sha256": "b" * 64})
        self.assertEqual([m.id for m in Catalog().by_installer_sha256("b" * 64)], ["com.example.app"])
        self.assertEqual(Catalog().by_installer_sha256("c" * 64), [])

    def test_shipped_policy(self):
        p = policy.Policy.load()
        self.assertEqual(p.problems, ())
        self.assertTrue(p.require_apparmor)
        self.assertTrue(p.unlisted_apps)
        self.assertEqual(p.unlisted_grants(), policy.Grants(network=False, display=True, audio=True, gpu=True))
        self.assertNotIn("blocked", p.allowed_statuses)

    def test_missing_policy_is_restrictive(self):
        (self.root / "etc/boswas/compat/policy.conf").unlink()
        p = policy.Policy.load()
        self.assertFalse(p.unlisted_apps)
        self.assertTrue(p.require_apparmor)
        self.assertEqual(p.allowed_statuses, frozenset({"approved", "tested"}))

    def test_invalid_values_fall_back_to_restrictive(self):
        self.write_policy('UNLISTED_APPS="maybe"\nREQUIRE_APPARMOR="sometimes"\nUNLISTED_NETWORK="perhaps"\n'
                          'MAX_INSTALLER_MB="lots"\nALLOWED_STATUSES="approved blocked bogus"\n')
        p = policy.Policy.load()
        self.assertFalse(p.unlisted_apps)
        self.assertTrue(p.require_apparmor)
        self.assertFalse(p.unlisted_network)
        self.assertEqual(p.max_installer_bytes, 0)
        self.assertEqual(p.allowed_statuses, frozenset({"approved"}))
        self.assertGreaterEqual(len(p.problems), 4)
        with self.assertRaises(Refused):
            p.check_status("blocked", "x.y")


# --- installer inspection ----------------------------------------------------------------------

class InstallerTests(CompatTestCase):
    def test_pe_architectures_and_framework(self):
        exe = self.installer_file()
        info = installer.inspect(exe)
        self.assertEqual((info.kind, info.machine, info.subsystem, info.framework), ("exe", "x86_64", "console", "NSIS"))
        self.assertEqual(info.framework_silent_args, ("/S",))
        x86 = self.installer_file("x86.exe", pe_bytes(machine=0x14C, subsystem=2))
        self.assertEqual((installer.inspect(x86).machine, installer.inspect(x86).subsystem), ("x86", "gui"))

    def test_msi_and_unknown_formats(self):
        msi = self.installer_file("a.msi", installer.OLE_MAGIC + b"\0" * 600)
        self.assertEqual(installer.inspect(msi).kind, "msi")
        for name, data in (("script.exe", b"#!/bin/sh\nrm -rf ~\n"), ("dos.exe", b"MZ" + b"\0" * 200)):
            with self.assertRaises(Refused):
                installer.inspect(self.installer_file(name, data))

    def test_copy_and_hash(self):
        src = self.installer_file()
        sha, size = installer.copy_and_hash(src, self.tmp / "copy", 10 * 1024 * 1024)
        self.assertEqual(sha, hashlib.sha256(src.read_bytes()).hexdigest())
        self.assertEqual(size, src.stat().st_size)
        self.assertEqual(stat.S_IMODE((self.tmp / "copy").stat().st_mode), 0o600)
        with self.assertRaises(Refused):
            installer.copy_and_hash(src, self.tmp / "copy2", 100)


# --- sandbox --------------------------------------------------------------------------------------

class SandboxTests(CompatTestCase):
    def build(self, grants, session=None, env=None):
        appdir = self.home / ".local/share/boswas/wine/com.example.app"
        session = session or sandbox.HostSession(uid=4242, username="tester", home=self.home)
        return sandbox.build_command(app_id="com.example.app", appdir=appdir, session=session, grants=grants,
                                     runtime=sandbox.Runtime(), command=["/usr/lib/wine/wine64", "app.exe"],
                                     extra_env=env)

    @staticmethod
    def env_of(argv):
        return {argv[i + 1]: argv[i + 2] for i, a in enumerate(argv) if a == "--setenv"}

    def test_isolation_defaults(self):
        argv = self.build(policy.Grants())
        for flag in ("--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session",
                     "--clearenv"):
            self.assertIn(flag, argv)
        self.assertNotIn("--share-net", argv)
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        # The home directory is never mounted; only the application's sandbox dir.
        binds = [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a in ("--bind", "--ro-bind", "--dev-bind")]
        self.assertEqual([b for b in binds if b[0].startswith(str(self.home))],
                         [(str(self.home / ".local/share/boswas/wine/com.example.app/sandbox"),
                           "/var/lib/boswas/wine/com.example.app")])
        self.assertFalse(any("/home" == dest or dest.startswith("/run/user/4242/bus") or "dbus" in dest
                             for _, dest in binds))
        self.assertEqual(argv[-3:], [paths.RUNNER, "/usr/lib/wine/wine64", "app.exe"])
        env = self.env_of(argv)
        self.assertEqual(env["WINEPREFIX"], "/var/lib/boswas/wine/com.example.app/prefix")
        self.assertEqual(env["HOME"], "/var/lib/boswas/wine/com.example.app/home")
        self.assertIn("winemenubuilder.exe=d", env["WINEDLLOVERRIDES"])
        self.assertNotIn("DISPLAY", env)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)

    def test_network_grant(self):
        self.assertIn("--share-net", self.build(policy.Grants(network=True)))

    def test_display_grant_binds_only_the_x11_socket(self):
        sock = self.root / "tmp/.X11-unix/X0"
        sock.parent.mkdir(parents=True)
        sock.write_text("")
        xauth = self.tmp / "xauth"
        xauth.write_text("cookie")
        session = sandbox.HostSession(uid=os.getuid(), username="tester", home=self.home, display=":0",
                                      xauthority=xauth)
        argv = self.build(policy.Grants(display=True), session)
        self.assertIn("/tmp/.X11-unix/X0", argv)
        env = self.env_of(argv)
        self.assertEqual(env["DISPLAY"], ":0")
        self.assertEqual(env["XAUTHORITY"], f"/run/user/{os.getuid()}/X11/Xauthority")
        self.assertNotIn("DISPLAY", self.env_of(self.build(policy.Grants(display=False), session)))
        self.assertIsNone(sandbox.x11_socket("remote.example:10.0"))

    def test_folder_grants(self):
        (self.home / "Documents").mkdir()
        argv = self.build(policy.Grants(folders=("documents", "downloads")))
        self.assertIn(str((self.home / "Documents").resolve()), argv)
        self.assertIn("/var/lib/boswas/wine/com.example.app/home/Documents", argv)
        self.assertFalse(any("Downloads" in a for a in argv))          # does not exist: not bound
        # A user-dirs entry pointing at the home directory itself is never bound.
        (self.home / ".config").mkdir()
        (self.home / ".config/user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/"\n')
        self.assertIsNone(sandbox.user_folder(self.home, "documents"))

    def test_manifest_environment_keeps_mandatory_overrides(self):
        env = self.env_of(self.build(policy.Grants(), env={"WINEDLLOVERRIDES": "d3d11=n", "APP_MODE": "1",
                                                           "HOME": "/home/tester", "LD_PRELOAD": "/tmp/x.so"}))
        self.assertEqual(env["APP_MODE"], "1")
        self.assertEqual(env["HOME"], "/var/lib/boswas/wine/com.example.app/home")   # never overridden
        self.assertNotIn("LD_PRELOAD", env)
        self.assertTrue(env["WINEDLLOVERRIDES"].startswith("d3d11=n;"))
        self.assertIn("winemenubuilder.exe=d", env["WINEDLLOVERRIDES"])


# --- store ------------------------------------------------------------------------------------------

class StoreTests(CompatTestCase):
    def test_layout_and_records(self):
        s = store.AppStore(self.home)
        s.ensure_root()
        appdir = s.create("com.example.app")
        for sub in ("", "logs", "sandbox", "sandbox/prefix", "sandbox/home"):
            self.assertEqual(stat.S_IMODE((appdir / sub).stat().st_mode), 0o700, sub)
        s.write_record("com.example.app", {"id": "com.example.app", "state": "installed"})
        self.assertEqual(s.read_record("com.example.app")["schema"], "boswas-winapp-record/1")
        self.assertEqual(stat.S_IMODE((appdir / "app.json").stat().st_mode), 0o600)
        with self.assertRaises(Refused):
            s.create("com.example.app")
        (s.root / "Not-An-Id").mkdir()
        os.symlink(appdir, s.root / "link.app")
        self.assertEqual(s.list_ids(), ["com.example.app"])

    def test_records_are_never_read_through_symlinks(self):
        s = store.AppStore(self.home)
        s.ensure_root()
        appdir = s.create("com.example.app")
        secret = self.tmp / "secret.json"
        secret.write_text('{"id": "com.example.app", "secret": 1}')
        os.symlink(secret, appdir / "app.json")
        self.assertIsNone(s.read_record("com.example.app"))

    def test_lock_and_logs(self):
        s = store.AppStore(self.home)
        s.ensure_root()
        s.create("com.example.app")
        with s.lock("com.example.app"):
            self.assertTrue(s.is_locked("com.example.app"))
            with self.assertRaises(Busy):
                with s.lock("com.example.app"):
                    pass
        self.assertFalse(s.is_locked("com.example.app"))
        for _ in range(store.KEEP_LOGS + 3):
            s.new_log("com.example.app", "launch")
        self.assertEqual(len(s.logs("com.example.app", "launch")), store.KEEP_LOGS)

    def test_all_users_inventory_needs_root_and_reports_only_inventory_fields(self):
        with mock.patch.object(os, "geteuid", return_value=1000):
            with self.assertRaises(Refused):
                store.list_all_users()
        s = store.AppStore(self.home)
        s.ensure_root()
        s.create("com.example.app")
        s.write_record("com.example.app", {"id": "com.example.app", "name": "X", "installer": {"sha256": "a" * 64}})
        entry = mock.Mock(pw_uid=os.getuid(), pw_dir=str(self.home), pw_name="tester")
        with mock.patch.object(os, "geteuid", return_value=0), mock.patch("pwd.getpwall", return_value=[entry]):
            users = store.list_all_users(min_uid=0)
        self.assertEqual(users[0]["user"], "tester")
        self.assertEqual(set(users[0]["applications"][0]), set(store.INVENTORY_FIELDS))


# --- operations --------------------------------------------------------------------------------------

class InstallTests(CompatTestCase):
    def test_unlisted_install(self):
        ctx = self.ctx()
        result = ops.install(ctx, str(self.installer_file("Fake Setup 1.2.exe")))
        app = result["application"]
        self.assertEqual((app["id"], app["status"], app["state"], app["manifest_source"]),
                         ("local.fake-setup-1-2", "unknown", "installed", "unlisted"))
        self.assertEqual(app["launch"], "C:\\Program Files\\Fake App\\fake.exe")   # uninstaller ignored
        self.assertEqual(result["sandbox"]["network"], False)
        installer_call = next(c for c in self.executor.calls if c["title"] == "installer")
        self.assertEqual(installer_call["command"][-1], "/S")                        # NSIS detected
        self.assertTrue(Path(result["desktop_entry"]).read_text().count("Exec=boswas-winapp launch local.fake-setup-1-2"))
        appdir = self.home / ".local/share/boswas/wine/local.fake-setup-1-2"
        self.assertEqual(list((appdir / "sandbox/installer").iterdir()), [])          # installer copy removed
        self.assertFalse((self.home / ".local/share/boswas/wine/.staging").exists() and
                         any((self.home / ".local/share/boswas/wine/.staging").iterdir()))
        # Every Windows step ran through the runner inside bwrap.
        for call in self.executor.calls:
            self.assertEqual(call["argv"][0], paths.BWRAP)
            self.assertIn(paths.RUNNER, call["argv"])

    def test_refused_as_root(self):
        ctx = self.ctx()
        with (mock.patch.object(ops, "require_user", REAL_REQUIRE_USER),
              mock.patch.object(os, "geteuid", return_value=0)):
            for operation in (lambda: ops.install(ctx, str(self.installer_file())),
                              lambda: ops.launch(ctx, "local.any", []),
                              lambda: ops.repair(ctx, "local.any"),
                              lambda: ops.remove(ctx, "local.any")):
                with self.assertRaises(Refused) as cm:
                    operation()
                self.assertEqual(cm.exception.reason, "root")
        self.assertEqual(self.executor.calls, [])

    def test_require_user_rejects_mismatched_ids(self):
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch.object(os, "getuid", return_value=1001):
            with self.assertRaises(Refused):
                REAL_REQUIRE_USER()
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch.object(os, "getuid", return_value=1000):
            REAL_REQUIRE_USER()

    def test_blocked_installer_is_refused_before_anything_runs(self):
        exe = self.installer_file()
        self.add_manifest(status="blocked", installer={"sha256": hashlib.sha256(exe.read_bytes()).hexdigest()})
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(exe), app_id="local.other-name")
        self.assertEqual(cm.exception.reason, "blocked")
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(store.AppStore(self.home).list_ids(), [])

    def test_unlisted_denied_by_policy(self):
        self.set_policy(UNLISTED_APPS="deny")
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(self.installer_file()))
        self.assertEqual(cm.exception.reason, "unlisted-denied")

    def test_catalog_match_by_hash_uses_manifest_sandbox(self):
        exe = self.installer_file()
        sha = hashlib.sha256(exe.read_bytes()).hexdigest()
        self.add_manifest(status="tested", installer={"sha256": sha, "silentArgs": ["/quiet"]},
                          launch="Program Files/Fake App/fake.exe", sandbox={"network": True, "display": False})
        result = ops.install(self.ctx(), str(exe))
        self.assertEqual((result["application"]["id"], result["application"]["status"]), ("com.example.app", "tested"))
        self.assertEqual(result["sandbox"]["network"], True)
        installer_call = next(c for c in self.executor.calls if c["title"] == "installer")
        self.assertEqual(installer_call["command"][-1], "/quiet")
        self.assertIn("--share-net", installer_call["argv"])

    def test_pinned_manifest_rejects_other_installer(self):
        self.add_manifest(status="tested", installer={"sha256": "a" * 64})
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(self.installer_file()), app_id="com.example.app")
        self.assertEqual(cm.exception.reason, "installer-mismatch")

    def test_manifest_launch_target_must_exist(self):
        exe = self.installer_file()
        self.add_manifest(status="tested", installer={"sha256": hashlib.sha256(exe.read_bytes()).hexdigest()},
                          launch="C:\\Program Files\\Missing\\missing.exe")
        with self.assertRaises(WinAppError):
            ops.install(self.ctx(), str(exe))
        record = store.AppStore(self.home).read_record("com.example.app")
        self.assertEqual(record["state"], "failed")

    def test_32_bit_programs_are_refused(self):
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(self.installer_file("x86.exe", pe_bytes(machine=0x14C))))
        self.assertEqual(cm.exception.reason, "architecture")
        self.assertEqual(self.executor.calls, [])

    def test_unavailable_runtime_components_are_refused(self):
        exe = self.installer_file()
        sha = hashlib.sha256(exe.read_bytes()).hexdigest()
        self.add_manifest(installer={"sha256": sha}, dependencies=["vcrun2019"])
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(exe))
        self.assertEqual(cm.exception.reason, "dependencies")
        self.add_manifest(installer={"sha256": sha}, winetricks=["corefonts"])
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(exe))
        self.assertEqual(cm.exception.reason, "winetricks")

    def test_status_not_allowed_by_policy(self):
        exe = self.installer_file()
        self.add_manifest(status="experimental", installer={"sha256": hashlib.sha256(exe.read_bytes()).hexdigest()})
        self.set_policy(ALLOWED_STATUSES="approved tested")
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(exe))
        self.assertEqual(cm.exception.reason, "status-not-allowed")

    def test_duplicate_and_ambiguous(self):
        ops.install(self.ctx(), str(self.installer_file()), app_id="local.tool")
        with self.assertRaises(Refused) as cm:
            ops.install(self.ctx(), str(self.installer_file()), app_id="local.tool")
        self.assertEqual(cm.exception.reason, "already-installed")
        self.executor.install_creates = ["a/one.exe", "b/two.exe"]
        result = ops.install(self.ctx(), str(self.installer_file()), app_id="local.multi")
        self.assertIsNone(result["application"]["launch"])
        self.assertEqual(len(result["application"]["launch_candidates"]), 2)
        self.assertIsNone(result["desktop_entry"])

    def test_portable_copies_the_program(self):
        result = ops.install(self.ctx(), str(self.installer_file("tool.exe")), portable=True)
        self.assertEqual(result["application"]["launch"], "C:\\Program Files\\local.tool\\tool.exe")
        self.assertFalse(any(c["title"] == "installer" for c in self.executor.calls))

    def test_runner_refusal_and_missing_confinement(self):
        self.executor.refused = "AppArmor profile boswas-winapp is not enforcing"
        with self.assertRaises(Unavailable):
            ops.install(self.ctx(), str(self.installer_file()), app_id="local.a")
        self.executor.refused = None
        self.executor.confinement = "unconfined"
        with self.assertRaises(Unavailable):
            ops.install(self.ctx(), str(self.installer_file()), app_id="local.b")
        # Environments without AppArmor may opt out by policy.
        self.set_policy(REQUIRE_APPARMOR="no")
        ops.install(self.ctx(), str(self.installer_file()), app_id="local.c")

    def test_installer_failure_marks_record(self):
        self.executor.exit_code = 3
        with self.assertRaises(WinAppError):
            ops.install(self.ctx(), str(self.installer_file()), app_id="local.broken")
        self.assertEqual(store.AppStore(self.home).read_record("local.broken")["state"], "failed")
        with self.assertRaises(Refused):
            ops.launch(self.ctx(), "local.broken", [])


class LaunchAndManageTests(CompatTestCase):
    def setUp(self):
        super().setUp()
        self.exe = self.installer_file()
        ops.install(self.ctx(), str(self.exe), app_id="local.fake")
        self.executor.calls.clear()

    def test_launch(self):
        out = io.StringIO()
        result = ops.launch(self.ctx(), "local.fake", ["read", "Z:/etc/hostname"], mirror=out)
        call = self.executor.calls[-1]
        self.assertEqual(call["command"], ["/usr/lib/wine/wine64", "C:\\Program Files\\Fake App\\fake.exe",
                                           "read", "Z:/etc/hostname"])
        self.assertEqual(call["argv"][call["argv"].index("--chdir") + 1],
                         "/var/lib/boswas/wine/local.fake/prefix/drive_c/Program Files/Fake App")
        self.assertEqual((result["exit_code"], result["confinement"]), (0, ENFORCED))
        self.assertIn("hello from windows", out.getvalue())
        record = store.AppStore(self.home).read_record("local.fake")
        self.assertEqual(record["last_launch"]["confinement"], ENFORCED)

    def test_launch_is_exclusive(self):
        s = store.AppStore(self.home)
        with s.lock("local.fake"):
            with self.assertRaises(Busy):
                ops.launch(self.ctx(), "local.fake", [])

    def test_policy_is_rechecked_at_launch(self):
        self.add_manifest(id="block.fake", status="blocked",
                          installer={"sha256": hashlib.sha256(self.exe.read_bytes()).hexdigest()})
        with self.assertRaises(Refused):
            ops.launch(self.ctx(), "local.fake", [])
        self.assertEqual(self.executor.calls, [])

    def test_record_edits_do_not_widen_the_sandbox(self):
        s = store.AppStore(self.home)
        record = s.read_record("local.fake")
        record.update(status="approved", manifest={"source": "unlisted"}, sandbox={"network": True})
        s.write_record("local.fake", record)
        ops.launch(self.ctx(), "local.fake", [])
        self.assertNotIn("--share-net", self.executor.calls[-1]["argv"])
        self.assertEqual(ops.status(self.ctx(), "local.fake")["application"]["status"], "unknown")

    def test_exe_override_stays_inside_the_prefix(self):
        with self.assertRaises(UsageError):
            ops.launch(self.ctx(), "local.fake", [], exe="D:\\evil.exe")
        with self.assertRaises(UsageError):
            ops.launch(self.ctx(), "local.fake", [], exe="..\\..\\x.exe")
        with self.assertRaises(NotFound):
            ops.launch(self.ctx(), "local.fake", [], exe="C:\\nope\\x.exe")
        # Symbolic links planted by the application are never followed.
        drive_c = self.home / ".local/share/boswas/wine/local.fake/sandbox/prefix/drive_c"
        os.symlink("/bin/sh", drive_c / "Program Files/Fake App/link.exe")
        with self.assertRaises(NotFound):
            ops.launch(self.ctx(), "local.fake", [], exe="C:\\Program Files\\Fake App\\link.exe")

    def test_status_list_logs(self):
        st = ops.status(self.ctx(), "local.fake")
        self.assertEqual((st["policy"]["allowed"], st["running"]), (True, False))
        self.assertEqual(st["paths"]["sandbox_view"], "/var/lib/boswas/wine/local.fake")
        listed = ops.list_apps(self.ctx())["applications"]
        self.assertEqual([a["id"] for a in listed], ["local.fake"])
        ops.launch(self.ctx(), "local.fake", [])
        logs = ops.logs(self.ctx(), "local.fake", kind="launch")
        self.assertTrue(any("red output" in line for line in logs["lines"]))
        self.assertFalse(any("\x1b" in line for line in logs["lines"]))
        with self.assertRaises(NotFound):
            ops.status(self.ctx(), "local.nothing")

    def test_repair(self):
        appdir = self.home / ".local/share/boswas/wine/local.fake"
        shutil.rmtree(appdir / "sandbox/home")
        result = ops.repair(self.ctx(), "local.fake")
        self.assertTrue(result["healthy"])
        self.assertTrue((appdir / "sandbox/home").is_dir())
        self.assertTrue(any("wineboot" in c["command"] for c in self.executor.calls))
        (appdir / "sandbox/prefix/drive_c/Program Files/Fake App/fake.exe").unlink()
        result = ops.repair(self.ctx(), "local.fake")
        self.assertFalse(result["healthy"])
        self.assertEqual(result["state"], "failed")

    def test_remove(self):
        entry = self.home / ".local/share/applications/boswas-winapp-local.fake.desktop"
        self.assertTrue(entry.exists())
        ops.remove(self.ctx(), "local.fake")
        self.assertFalse((self.home / ".local/share/boswas/wine/local.fake").exists())
        self.assertFalse(entry.exists())
        with self.assertRaises(NotFound):
            ops.remove(self.ctx(), "local.fake")


# --- executor and CLI ----------------------------------------------------------------------------------

class ExecutorTests(CompatTestCase):
    def script(self, body):
        p = self.tmp / "fake-bwrap"
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)
        return [str(p)]

    def test_output_is_sanitised_and_confinement_parsed(self):
        argv = self.script("echo 'boswas-winapp-exec: confinement=boswas-winapp (enforce)' >&2\n"
                           "printf '\\033]0;evil title\\007hello \\033[2Jworld\\n'\nexit 5\n")
        out = io.StringIO()
        result = executor.Executor().run(argv, self.tmp / "log", mirror=out)
        self.assertEqual((result.exit_code, result.confinement, result.refused), (5, ENFORCED, None))
        self.assertEqual(out.getvalue(), "hello world\n")
        self.assertNotIn("\x1b", (self.tmp / "log").read_text())

    def test_only_the_runner_can_refuse(self):
        marker = "echo 'boswas-winapp-exec: refused: not enforcing' >&2\n"
        self.assertIsNotNone(executor.Executor().run(self.script(marker + "exit 77\n"), self.tmp / "l1").refused)
        self.assertIsNone(executor.Executor().run(self.script(marker + "exit 0\n"), self.tmp / "l2").refused)

    def test_timeout(self):
        result = executor.Executor().run(self.script("exec sleep 30\n"), self.tmp / "log", timeout=0.5)
        self.assertTrue(result.timed_out)


class CliTests(CompatTestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(ops.Context, "create", lambda out=None: self.ctx(out)):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_list_json_and_text(self):
        code, out, _ = self.run_cli("--json", "list")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual((doc["schema"], doc["command"], doc["applications"]), ("boswas-winapp/1", "list", []))
        code, out, _ = self.run_cli("list")
        self.assertIn("No Windows applications installed", out)

    def test_install_launch_with_passthrough_arguments(self):
        exe = self.installer_file()
        code, out, _ = self.run_cli("--json", "install", str(exe), "--id", "local.cli")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["application"]["id"], "local.cli")
        self.executor.exit_code = 7
        code, out, _ = self.run_cli("launch", "local.cli", "--quiet", "--", "--flag", "read", "x")
        self.assertEqual(code, 7)
        self.assertEqual(self.executor.calls[-1]["command"][-3:], ["--flag", "read", "x"])

    def test_errors_have_stable_exit_codes(self):
        code, out, _ = self.run_cli("--json", "install", str(self.tmp / "missing.exe"))
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["error"]["reason"], "not-found")
        code, _, err = self.run_cli("status", "local.none")
        self.assertEqual(code, 3)
        code, _, err = self.run_cli("install", str(self.installer_file("x86.exe", pe_bytes(machine=0x14C))))
        self.assertEqual(code, 4)
        self.assertIn("x86", err)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["list", "--", "x"])
        self.assertEqual(cm.exception.code, 2)
        code, _, _ = self.run_cli()
        self.assertEqual(code, 2)

    def test_manifest_validate(self):
        good = COMPAT / "manifests/examples/example.application.json"
        code, out, _ = self.run_cli("--json", "manifest", "validate", str(good))
        self.assertEqual(code, 0, out)
        bad = self.tmp / "bad.app.json"
        bad.write_text(json.dumps({"id": "bad.app", "environment": {"LD_PRELOAD": "x"}}))
        code, out, _ = self.run_cli("manifest", "validate", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("LD_PRELOAD", out)


if __name__ == "__main__":
    unittest.main()
