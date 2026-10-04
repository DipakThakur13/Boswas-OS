#!/usr/bin/env python3
"""Device management end to end inside the Boswas OS image root.

Started by test_device_runtime.sh as root inside a private mount namespace
whose root is a throw-away overlay of the extracted image (like the WinCompat
runtime test). Everything is the image's own code: the device agent service,
a user's session agent, boswas-winapp with real Wine 10 and bubblewrap, and
the Compatibility Manager's backend calls. The Control Plane runs from the
repository sources on 127.0.0.1 with the image's Python.

Covers: persistent identity, local API, local install/launch/stop/logs/
repair/remove through the session agent, 32-bit and blocked refusals,
enrollment, heartbeat, signed policy, remote INSTALL/LAUNCH/REMOVE with the
installer downloaded over mutual TLS, 32-bit catalog entries refused,
policy blocks, offline operation, privacy of the inventory, the audit
trail, and clean shutdown.

Prints one line per result: PASS|FAIL|SKIP|INFO <TAB> description.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/boswas-test")
from cp_harness import ControlPlane  # noqa: E402

FIX = Path("/opt/boswas-test/fixtures")
CP_SRC = Path("/opt/boswas-test/control-plane")
USER, UID = "devtest", 1600
HOME = Path(f"/home/{USER}")
RUN = Path(f"/run/user/{UID}")
CP_PORT = 18443
CP_URL = f"https://127.0.0.1:{CP_PORT}"
CP_DATA = Path("/root/cp-data")
LOGS = Path("/root/device-logs")
APP, LONG = "com.boswas.testapp", "com.boswas.testapp-long"
MESSAGE_32 = "This application requires 32-bit Windows compatibility, which is not supported by Boswas OS."
processes: dict[str, subprocess.Popen] = {}
controlplanes: list = []


def emit(status: str, text: str) -> None:
    print(f"{status}\t{text}", flush=True)


def check(condition: bool, text: str, detail: object = "") -> bool:
    detail = detail if isinstance(detail, str) else json.dumps(detail, default=str)
    emit("PASS" if condition else "FAIL", text if condition or not detail else f"{text} [{detail[-700:]}]")
    return condition


def user_env() -> dict:
    return {"PATH": "/usr/bin:/bin", "HOME": str(HOME), "USER": USER, "LOGNAME": USER, "LANG": "C.UTF-8",
            "XDG_RUNTIME_DIR": str(RUN)}


def run(args, *, user=False, timeout=600, env=None):
    kwargs = dict(user=UID, group=UID, extra_groups=[], cwd=str(HOME)) if user else {}
    try:
        return subprocess.run(args, env={**(user_env() if user else {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                                                                     "LANG": "C.UTF-8"}), **(env or {})},
                              capture_output=True, text=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, 124, exc.stdout or "", f"timeout after {timeout} s")


def session(op: str, params: dict | None = None, *, job: bool = False, timeout: int = 1800) -> dict:
    """An operation on the test user's session agent, as that user (like the Compatibility Manager)."""
    args = ["python3", "-B", "/opt/boswas-test/session_client.py", *(["--job"] if job else []), op,
            json.dumps(params or {})]
    proc = run(args, user=True, timeout=timeout)
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": {"code": "CLIENT", "message": (proc.stdout + proc.stderr)[-500:]}}


def device(*args: str) -> tuple[int, dict]:
    proc = run(["boswas-device", "--json", *args], timeout=180)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"raw": proc.stdout + proc.stderr}


def wait_for(predicate, timeout: float, interval: float = 1.0, nudge=None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if nudge:
            nudge()
        time.sleep(interval)
    return predicate()


def apparmor_available() -> bool:
    try:
        return Path("/sys/module/apparmor/parameters/enabled").read_text().strip() == "Y" and \
            Path("/sys/kernel/security/apparmor/profiles").exists()
    except OSError:
        return False


def relax_managed_policy_for_test_kernel(enforcing: bool) -> None:
    """Test environment without AppArmor (Docker Desktop's WSL2 kernel), as in the
    WinCompat runtime test: let Windows code run unconfined. A signed policy can
    never do this itself; the QEMU boot test covers the confined path."""
    if enforcing:
        return
    path = Path("/var/lib/boswas/compat/policy.conf")
    if path.exists():
        path.write_text(path.read_text().replace('REQUIRE_APPARMOR="yes"', 'REQUIRE_APPARMOR="no"'))


def start(name: str, args: list[str], *, user: bool = False, env: dict | None = None) -> subprocess.Popen:
    LOGS.mkdir(exist_ok=True)
    log = open(LOGS / f"{name}.log", "w")
    kwargs = dict(user=UID, group=UID, extra_groups=[], cwd=str(HOME)) if user else {}
    proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                            env={**(user_env() if user else {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                                                              "LANG": "C.UTF-8"}), **(env or {})}, **kwargs)
    processes[name] = proc
    return proc


def stop(name: str, timeout: float = 30) -> int | None:
    proc = processes.pop(name, None)
    if proc is None or proc.poll() is not None:
        return proc.returncode if proc else None
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return None


def setup() -> bool:
    if subprocess.run(["getent", "passwd", USER], capture_output=True).returncode != 0:
        subprocess.run(["useradd", "-m", "-u", str(UID), "-s", "/bin/bash", USER], check=True, capture_output=True)
    RUN.mkdir(parents=True, exist_ok=True)
    os.chown(RUN, UID, UID)
    RUN.chmod(0o700)
    downloads = HOME / "Downloads"
    downloads.mkdir(exist_ok=True)
    for exe in FIX.glob("*.exe"):
        shutil.copy(exe, downloads / exe.name)
    subprocess.run(["chown", "-R", f"{UID}:{UID}", str(HOME)], check=True)
    for manifest in (FIX / "manifests").glob("*.json"):
        shutil.copy(manifest, Path("/etc/boswas/compat/manifests") / manifest.name)
    enforcing = apparmor_available()
    if enforcing:
        subprocess.run(["apparmor_parser", "-r", "-W", "/etc/apparmor.d/boswas-winapp"], capture_output=True)
    else:
        emit("SKIP", "AppArmor enforcement during device-management operations (this kernel has no AppArmor; the "
                     "QEMU boot test runs them confined)")
        with open("/etc/boswas/compat/policy.conf", "a") as fh:
            fh.write('\n# test environment without AppArmor\nREQUIRE_APPARMOR="no"\n')
    return enforcing


def main() -> int:
    enforcing = setup()

    # --- Control Plane (repository sources, image Python) ----------------------------------------
    LOGS.mkdir(exist_ok=True)
    cp = ControlPlane(python_path=[CP_SRC, "/usr/lib/boswas/python"], data_dir=CP_DATA, port=CP_PORT,
                      public_url=CP_URL, dashboard=CP_SRC / "dashboard", log=LOGS / "control-plane.log")
    try:
        cp.init(["127.0.0.1"])
    except RuntimeError as exc:
        check(False, "Control Plane initialises (boswas-cp init)", str(exc))
        return 1
    check(True, "Control Plane initialises: CAs, server certificate, policy key, default signed policy")
    controlplanes.append(cp)
    op = cp.request
    try:
        cp.start()
        check(True, "Control Plane serves its HTTPS API")
    except RuntimeError as exc:
        check(False, "Control Plane serves its HTTPS API", str(exc))
        return 1

    # --- the device agent service -----------------------------------------------------------------------
    shutil.copy(CP_DATA / "ca/server-ca.crt", "/etc/boswas/control-plane-ca.pem")
    os.chmod("/etc/boswas/control-plane-ca.pem", 0o644)
    conf = Path("/etc/boswas/device.conf")
    conf.write_text(conf.read_text().replace('CONTROL_PLANE_URL=""', f'CONTROL_PLANE_URL="{CP_URL}"')
                    .replace('CONTROL_PLANE_CA=""', 'CONTROL_PLANE_CA="/etc/boswas/control-plane-ca.pem"')
                    .replace('HEARTBEAT_INTERVAL="300"', 'HEARTBEAT_INTERVAL="30"'))
    rc, validated = device("config", "validate")
    check(rc == 0 and validated.get("config", {}).get("valid"), "device.conf with a Control Plane validates", validated)
    check(not Path("/var/lib/boswas/agent/identity.json").exists(), "the image carries no device identity")
    start("agent", ["/usr/lib/boswas/agent/boswas-device-agent"])
    if not check(wait_for(lambda: Path("/run/boswas-agent/agent.sock").exists(), 120),
                 "boswas-device-agent starts and serves its local API"):
        return 1
    rc, ident = device("identity")
    device_id = ident.get("identity", {}).get("device_id")
    check(rc == 0 and device_id and ident["identity"]["source"] == "generated",
          "a random device identity is created at the first start", ident)
    check(oct(Path("/var/lib/boswas/agent/identity.json").stat().st_mode & 0o777) == "0o644" and
          json.loads(Path("/var/lib/boswas/agent/identity.json").read_text())["device_id"] == device_id,
          "the identity is persisted (identity.json, 0644)")

    def agent_status() -> dict:
        return device("status")[1].get("agent", {})
    check(wait_for(lambda: agent_status().get("state") in ("READY", "DEGRADED"), 120), "device state settles",
          agent_status())
    status = agent_status()
    check(status.get("connection") == "UNENROLLED", "connection UNENROLLED before enrollment", status)

    # --- the user's session agent (Compatibility Manager backend) ------------------------------------
    start("session-agent", ["/usr/lib/boswas/agent/boswas-session-agent"], user=True)
    check(wait_for(lambda: (RUN / "boswas/session.sock").exists(), 60), "the user's session agent starts")
    check(wait_for(lambda: agent_status().get("sessions", {}).get("connected") == 1, 60),
          "the session agent registers with the device agent (kernel peer credentials)")
    forbidden = run(["python3", "-B", "-c", "import sys; sys.path.insert(0, '/usr/lib/boswas/python')\n"
                     "from boswas_agent.localapi import ApiClient, ApiError\n"
                     f"try:\n    ApiClient('{RUN}/boswas/session.sock').call('apps.list')\n    print('ALLOWED')\n"
                     "except (ApiError, ConnectionError) as e:\n    print(getattr(e, 'code', 'NO-ACCESS'))"])
    check(forbidden.stdout.strip() in ("FORBIDDEN", "NO-ACCESS"), "another user (root) cannot use the session agent",
          forbidden.stdout)
    sysstat = session("system.status")
    st = sysstat.get("result", {})
    check(sysstat.get("ok") and st.get("windows_architectures") == ["x86_64"] and st.get("agent", {}).get("state"),
          "system status: x86_64-only Windows runtime, device agent reachable", sysstat)
    check(st.get("runtime", {}).get("bubblewrap", {}).get("available") is True and
          st.get("runtime", {}).get("wine", {}).get("available") is True, "system status: Wine and bubblewrap available",
          st.get("runtime"))

    # --- local management through the session agent ----------------------------------------------
    downloads = HOME / "Downloads"
    x86 = session("apps.inspect", {"path": str(downloads / "boswas-testapp-x86.exe")})
    decision = x86.get("result", {}).get("decision", {})
    check(x86.get("ok") and decision.get("allowed") is False and decision.get("reason") == "architecture" and
          MESSAGE_32 in decision.get("message", ""), "a 32-bit installer is refused by inspection", x86)
    refused = session("apps.install", {"path": str(downloads / "boswas-testapp-x86.exe")})
    check(not refused.get("ok") and refused["error"]["code"] == "ARCHITECTURE" and
          MESSAGE_32 in refused["error"]["message"], "installing a 32-bit installer is refused with the product message",
          refused)
    blocked = session("apps.install", {"path": str(downloads / "boswas-testapp-blocked.exe"), "id": "local.renamed"})
    check(not blocked.get("ok") and blocked["error"]["code"] == "BLOCKED", "a blocked installer is refused", blocked)
    started = time.monotonic()
    job = session("apps.install", {"path": str(downloads / "boswas-testapp.exe"), "id": LONG}, job=True)
    check(job.get("ok") and job["result"]["state"] == "succeeded",
          f"install through the session agent (real Wine) succeeds ({time.monotonic() - started:.0f} s)", job)
    apps = {a["id"]: a for a in session("apps.list").get("result", {}).get("applications", [])}
    check(apps.get(LONG, {}).get("app_state") == "INSTALLED" and apps[LONG].get("architecture") == "x86_64",
          "the library lists it as INSTALLED, x86_64", apps.get(LONG))
    launched = session("apps.launch", {"id": LONG})
    check(launched.get("ok") and launched["result"].get("started"), "launch through the session agent", launched)
    check(wait_for(lambda: {a["id"]: a for a in session("apps.list")["result"]["applications"]}[LONG]["app_state"]
                   == "RUNNING", 120, 2), "the application is RUNNING")
    stopped = session("apps.stop", {"id": LONG})
    check(stopped.get("ok") and stopped["result"].get("stopped") is True, "stop ends the running application", stopped)
    check(wait_for(lambda: {a["id"]: a for a in session("apps.list")["result"]["applications"]}[LONG]["app_state"]
                   == "STOPPED", 60, 2), "the application is STOPPED afterwards")
    logs = session("apps.logs", {"id": LONG, "kind": "launch"})
    check(logs.get("ok") and any("stopped on request" in line for line in logs["result"]["lines"]),
          "the launch log records the stop", logs)
    repaired = session("apps.repair", {"id": LONG}, job=True)
    check(repaired.get("ok") and repaired["result"]["state"] == "succeeded" and
          repaired["result"]["result"]["healthy"] is True, "repair through the session agent", repaired)
    removed = session("apps.remove", {"id": LONG}, job=True)
    check(removed.get("ok") and removed["result"]["state"] == "succeeded" and
          not (HOME / f".local/share/boswas/wine/{LONG}").exists(), "remove deletes the prefix", removed)
    session("apps.install", {"path": str(downloads / "boswas-testapp.exe"), "id": LONG}, job=True)

    # --- the Compatibility Manager itself (Qt offscreen) on the real backend -------------------------
    probe = run(["python3", "-B", "/opt/boswas-test/gui_probe.py", LONG], user=True, timeout=600,
                env={"QT_QPA_PLATFORM": "offscreen"})
    try:
        gui = json.loads(probe.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        gui = {}
    check(gui.get("listed") is True, "the Compatibility Manager window lists the user's Windows application",
          gui or probe.stderr)
    check(gui.get("system_loaded") and gui.get("windows_architectures") == ["x86_64"] and gui.get("agent_state"),
          "the Compatibility Manager shows system status (x86_64 only, device agent state)", gui)
    check(gui.get("launched") is True, "Launch in the Compatibility Manager starts the application (through the "
                                       "session agent and boswas-winapp)", gui)
    check(gui.get("stopped") is True, "Stop in the Compatibility Manager stops it", gui)

    # --- enrollment and the Control Plane ---------------------------------------------------------------
    token = cp.enrollment_token()
    token_file = Path("/root/enroll-token")
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)
    rc, enrolled = device("enroll", "--token-file", str(token_file))
    token_file.unlink()
    check(rc == 0 and enrolled.get("enrolled") and enrolled.get("device_id") == device_id,
          "enrollment with a one-time token over TLS", enrolled)

    def nudge():
        device("sync")
    check(wait_for(lambda: agent_status().get("connection") == "CONNECTED" and
                   (agent_status().get("policy") or {}).get("version") == "default-1", 120, 3, nudge),
          "heartbeat accepted; the signed default policy is verified and applied", agent_status())
    relax_managed_policy_for_test_kernel(enforcing)
    # The agent reports the applied version with its next heartbeat (sent right after applying).
    registry = lambda: op("GET", f"/api/v1/devices/{device_id}")   # noqa: E731
    wait_for(lambda: registry()[1].get("policy_current"), 60, 3, nudge)
    status, dev = registry()
    check(status == 200 and dev["connection"] == "online" and dev["policy_current"] and dev["architecture"] in
          ("amd64", None), "the Control Plane registry shows the device online with the current policy", dev)
    check(wait_for(lambda: (op("GET", f"/api/v1/devices/{device_id}/inventory")[1].get("document") or {})
                   .get("windows_applications"), 120, 3, nudge), "inventory reaches the Control Plane")
    inventory = op("GET", f"/api/v1/devices/{device_id}/inventory")[1]
    text = json.dumps(inventory)
    check(USER not in text and str(HOME) not in text, "the inventory contains no user names or home paths")
    check(any(a["id"] == LONG for a in inventory["document"]["windows_applications"]),
          "the inventory lists the user's Windows application (aggregated)")

    # --- remote management with typed commands -------------------------------------------------------------
    exe = (FIX / "boswas-testapp.exe").read_bytes()
    status, art = op("POST", "/api/v1/artifacts", raw=exe, name="boswas-testapp.exe")
    manifest = json.loads((FIX / "manifests" / f"{APP}.json").read_text())
    status2, app = op("POST", "/api/v1/applications", {"manifest": manifest})
    check(status == 201 and status2 == 201 and app.get("installable") and app["architecture_label"] ==
          "x86_64 / 64-bit", "catalog: installer stored, 64-bit application installable", app)

    def command(body: dict) -> tuple[int, dict]:
        return op("POST", f"/api/v1/devices/{device_id}/commands", body)

    def finished(command_id: str, timeout: float = 900) -> dict:
        result = {}

        def done():
            nonlocal result
            result = op("GET", f"/api/v1/devices/{device_id}/commands/{command_id}")[1]
            return result.get("status") in ("SUCCEEDED", "FAILED", "EXPIRED", "CANCELLED")
        wait_for(done, timeout, 5, nudge)
        return result

    status, cmd = command({"type": "INSTALL_APPLICATION", "application_id": APP})
    started = time.monotonic()
    result = finished(cmd.get("command_id", ""))
    check(result.get("status") == "SUCCEEDED" and (HOME / f".local/share/boswas/wine/{APP}/sandbox/prefix/system.reg")
          .exists(), f"remote INSTALL_APPLICATION: downloaded over mutual TLS, installed by boswas-winapp for the "
                     f"user ({time.monotonic() - started:.0f} s)", result)
    status, cmd = command({"type": "LAUNCH_APPLICATION", "application_id": APP})
    result = finished(cmd.get("command_id", ""))
    check(result.get("status") == "SUCCEEDED" and result.get("result", {}).get("started"),
          "remote LAUNCH_APPLICATION", result)
    status, cmd = command({"type": "REMOVE_APPLICATION", "application_id": APP})
    result = finished(cmd.get("command_id", ""))
    check(result.get("status") == "SUCCEEDED" and not (HOME / f".local/share/boswas/wine/{APP}").exists(),
          "remote REMOVE_APPLICATION", result)
    status, cmd = command({"type": "REMOVE_APPLICATION", "application_id": APP})
    result = finished(cmd.get("command_id", ""))
    check(result.get("status") == "SUCCEEDED" and result.get("result", {}).get("already") is True,
          "removing it again is idempotent", result)
    status, body = command({"type": "EXECUTE_SHELL_COMMAND", "command": "id"})
    check(status == 400 and body["error"]["code"] in ("UNSUPPORTED_COMMAND", "INVALID_REQUEST"),
          "the Control Plane has no shell command", body)
    x86_exe = (FIX / "boswas-testapp-x86.exe").read_bytes()
    status, art32 = op("POST", "/api/v1/artifacts", raw=x86_exe, name="legacy-setup.exe")
    legacy = {**manifest, "id": "com.boswas.legacy", "name": "Legacy 32-bit", "architecture": "x86",
              "installer": {**manifest["installer"], "sha256": art32.get("sha256")}}
    status, entry = op("POST", "/api/v1/applications", {"manifest": legacy})
    check(status == 201 and entry["support"] == "UNSUPPORTED_ARCHITECTURE" and not entry["installable"],
          "a 32-bit catalog entry is marked UNSUPPORTED_ARCHITECTURE", entry)
    status, body = command({"type": "INSTALL_APPLICATION", "application_id": "com.boswas.legacy"})
    check(status == 422 and body["error"]["code"] == "UNSUPPORTED_ARCHITECTURE",
          "a 32-bit application can never become an install command", body)
    status, body = op("POST", "/api/v1/applications", {"manifest": {**legacy, "id": "com.boswas.liar",
                                                                    "architecture": "x86_64"}})
    check(status == 422 and body["error"]["code"] == "ARCHITECTURE_MISMATCH",
          "a 32-bit installer cannot be declared x86_64", body)

    # --- centrally distributed policy -----------------------------------------------------------------
    base = op("GET", "/api/v1/policies/default")[1]["document"]
    blocking = {k: base[k] for k in ("name", "description", "compat", "agent", "updates")}
    blocking["compat"] = {**base["compat"], "blocked_applications": [LONG]}
    status, published = op("POST", "/api/v1/policies", blocking)
    check(status == 201 and published["version"] == "default-2", "a new signed policy version is published", published)
    check(wait_for(lambda: (agent_status().get("policy") or {}).get("version") == "default-2", 120, 3, nudge),
          "the device verifies and applies it")
    relax_managed_policy_for_test_kernel(enforcing)
    refused = session("apps.launch", {"id": LONG})
    check(not refused.get("ok") and "blocked by device policy" in refused["error"]["message"],
          "the policy blocks the application on the device (session agent refuses to launch it)", refused)
    unblock = {**blocking, "compat": {**base["compat"], "blocked_applications": []}}
    op("POST", "/api/v1/policies", unblock)
    check(wait_for(lambda: (agent_status().get("policy") or {}).get("version") == "default-3", 120, 3, nudge),
          "the next policy version lifts the block")
    relax_managed_policy_for_test_kernel(enforcing)

    # --- offline: the Control Plane must never block local work ----------------------------------
    cp.stop()
    for _ in range(4):
        nudge()
        time.sleep(2)
    check(wait_for(lambda: agent_status().get("connection") == "OFFLINE", 120, 3, nudge),
          "with the Control Plane down the agent reports OFFLINE", agent_status())
    check(agent_status().get("state") == "OFFLINE", "device state OFFLINE (local applications keep working)")
    launched = session("apps.launch", {"id": LONG})
    check(launched.get("ok") and launched["result"].get("started"),
          "a Windows application still launches while the Control Plane is unreachable", launched)
    check(session("apps.stop", {"id": LONG}).get("ok"), "and can be stopped")
    rc, inv = device("inventory")
    check(rc == 0 and inv.get("inventory", {}).get("os"), "the inventory is still collected locally", inv)
    cp.start()
    check(wait_for(lambda: agent_status().get("connection") == "CONNECTED", 180, 5, nudge),
          "the agent reconnects when the Control Plane is back", agent_status())

    # --- audit trail ------------------------------------------------------------------------------------
    events = op("GET", f"/api/v1/devices/{device_id}/events?limit=500")[1].get("events", [])
    types = {e["type"] for e in events}
    check({"DEVICE_REGISTERED", "DEVICE_ONLINE", "COMMAND_CREATED", "COMMAND_COMPLETED", "APPLICATION_INSTALLED",
           "APPLICATION_REMOVED", "APPLICATION_LAUNCHED"} <= types, "the audit trail records the management actions",
          sorted(types))
    verify = op("GET", "/api/v1/events/verify")[1]
    check(verify.get("valid") is True, f"the audit hash chain is intact ({verify.get('events')} events)", verify)
    check(token not in json.dumps(events) and token not in (LOGS / "agent.log").read_text(errors="replace"),
          "the enrollment token appears in no event or log")

    # --- clean shutdown --------------------------------------------------------------------------------
    check(stop("session-agent") == 0, "the session agent stops cleanly")
    check(stop("agent") == 0 and not Path("/run/boswas-agent/agent.sock").exists(),
          "the device agent stops cleanly and removes its socket")
    cp.stop()
    rc, ident2 = device("identity")
    check(ident2.get("identity", {}).get("device_id") == device_id, "the device identity survives the restart")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # keep the output parseable
        emit("FAIL", f"device scenario aborted: {type(exc).__name__}: {exc}")
        code = 1
    finally:
        for name in list(processes):
            stop(name, timeout=10)
        for plane in controlplanes:
            plane.stop()
    sys.exit(code)
