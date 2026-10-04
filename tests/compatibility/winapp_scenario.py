#!/usr/bin/env python3
"""WinCompat end-to-end scenario inside the Boswas OS image root.

Started by test_winapp_runtime.sh as root, inside a private mount namespace
whose root is a throw-away overlay of the extracted image. Every
boswas-winapp command runs as an unprivileged test user, exactly as on a
device: real Wine 10, real bubblewrap, the shipped boswas-compat package.

Prints one line per result: PASS|FAIL|SKIP|INFO <TAB> description.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

FIX = Path("/root/boswas-test/fixtures")
USER, UID = "wintest", 1500
HOME = Path(f"/home/{USER}")
DATA = HOME / ".local/share/boswas/wine"
LISTEN_PORT = 47011
APP, NET, UNLISTED = "com.boswas.testapp", "com.boswas.testapp-net", "local.unlisted-test"
ENFORCED = "boswas-winapp (enforce)"


def emit(status: str, text: str) -> None:
    print(f"{status}\t{text}", flush=True)


def check(condition: bool, text: str, detail: str = "") -> bool:
    emit("PASS" if condition else "FAIL", text if condition or not detail else f"{text} [{detail[-600:]}]")
    return condition


def run(args, *, user=True, timeout=900, env=None):
    base_env = {"PATH": "/usr/bin:/bin", "HOME": str(HOME), "USER": USER, "LOGNAME": USER, "LANG": "C.UTF-8",
                "XDG_RUNTIME_DIR": f"/run/user/{UID}"}
    kwargs = dict(user=UID, group=UID, extra_groups=[], cwd=str(HOME)) if user else {}
    try:
        return subprocess.run(args, env={**base_env, **(env or {})}, capture_output=True, text=True,
                              timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, 124, exc.stdout or "", f"timeout after {timeout} s")


def winapp(*args, **kw):
    return run(["boswas-winapp", *args], **kw)


def as_json(proc) -> dict:
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {}


def probes(output: str) -> dict[str, str]:
    """{'read Z:/x': 'BLOCKED', ...} from the test application's output."""
    result = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "PROBE":
            result[f"{parts[1]} {parts[2]}"] = parts[3]
    return result


def listener() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", LISTEN_PORT))
    srv.listen(16)
    while True:
        conn, _ = srv.accept()
        conn.close()


def apparmor_available() -> bool:
    try:
        return Path("/sys/module/apparmor/parameters/enabled").read_text().strip() == "Y" and \
            Path("/sys/kernel/security/apparmor/profiles").exists()
    except OSError:
        return False


def setup() -> bool:
    if subprocess.run(["getent", "passwd", USER], capture_output=True).returncode == 0:
        shutil.rmtree(HOME, ignore_errors=True)        # start from an empty home (overlay)
        HOME.mkdir(mode=0o750)
    else:
        subprocess.run(["useradd", "-m", "-u", str(UID), "-s", "/bin/bash", USER], check=True, capture_output=True)
    run_dir = Path(f"/run/user/{UID}")
    run_dir.mkdir(parents=True)
    os.chown(run_dir, UID, UID)
    run_dir.chmod(0o700)
    # Things a Windows application must never reach.
    for rel, content in ((".ssh/id_test", "SECRET-KEY-MATERIAL\n"), ("Documents/report.txt", "private report\n"),
                         (".bashrc", "# user shell config\n")):
        path = HOME / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    downloads = HOME / "Downloads"
    downloads.mkdir(exist_ok=True)
    for exe in FIX.glob("*.exe"):
        shutil.copy(exe, downloads / exe.name)
    subprocess.run(["chown", "-R", f"{UID}:{UID}", str(HOME)], check=True)
    for manifest in (FIX / "manifests").glob("*.json"):
        shutil.copy(manifest, Path("/etc/boswas/compat/manifests") / manifest.name)
    threading.Thread(target=listener, daemon=True).start()
    if apparmor_available():
        loaded = subprocess.run(["apparmor_parser", "-r", "-W", "/etc/apparmor.d/boswas-winapp"],
                                capture_output=True, text=True)
        check(loaded.returncode == 0, "AppArmor profile boswas-winapp loads into the running kernel", loaded.stderr)
        return True
    emit("SKIP", "AppArmor enforcement (this kernel has no AppArmor; verified by the QEMU boot test)")
    with open("/etc/boswas/compat/policy.conf", "a") as fh:
        fh.write('\n# test environment without AppArmor\nREQUIRE_APPARMOR="no"\n')
    return False


def owned_by_user(path: Path) -> list[str]:
    wrong = []
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            p = os.path.join(root, name)
            if os.lstat(p).st_uid != UID:
                wrong.append(p)
    return wrong


def main() -> int:
    enforcing = setup()
    exe = HOME / "Downloads/boswas-testapp.exe"

    # --- never as root ---------------------------------------------------------------
    p = winapp("install", str(exe), "--id", APP, user=False, env={"HOME": "/root"})
    check(p.returncode == 4 and "root" in p.stderr, "install as root is refused (exit 4)", p.stderr)
    check(not Path("/root/.local/share/boswas/wine").exists(), "nothing was created for root")

    # --- catalog ------------------------------------------------------------------------------
    p = winapp("--json", "catalog")
    ids = {m["id"]: m["status"] for m in as_json(p).get("manifests", [])}
    check(ids.get(APP) == "tested" and ids.get("com.boswas.testapp-blocked") == "blocked",
          "catalog lists the local manifests with their statuses", p.stdout + p.stderr)
    p = winapp("install", str(exe))
    check(p.returncode == 2 and "several catalog manifests" in p.stderr,
          "an installer pinned by two manifests needs --id (exit 2)", p.stderr)

    # --- install ----------------------------------------------------------------------------------
    started = time.monotonic()
    p = winapp("--json", "install", str(exe), "--id", APP)
    doc = as_json(p)
    app = doc.get("application", {})
    if not check(p.returncode == 0 and app.get("state") == "installed",
                 f"boswas-winapp install {exe.name} succeeds ({time.monotonic() - started:.0f} s)",
                 p.stdout + p.stderr):
        tail = run(["sh", "-c", f"tail -n 40 {DATA}/{APP}/logs/install-*.log"])
        emit("INFO", "install log: " + tail.stdout.replace("\n", " | ")[-1500:])
        return 1
    check(app.get("status") == "tested" and app.get("manifest_source") == "local",
          "installation matched the local catalog manifest (status tested)", json.dumps(app))
    check(app.get("launch") == "C:\\Program Files\\Boswas Test App\\boswas-testapp.exe",
          "launch target from the manifest resolved in the prefix", str(app.get("launch")))

    appdir = DATA / APP
    st = appdir.stat()
    check(st.st_uid == UID and (st.st_mode & 0o777) == 0o700, "application directory is private (0700, owned by the user)")
    check((appdir / "sandbox/prefix/system.reg").is_file(), f"isolated Wine prefix created ({appdir}/sandbox/prefix)")
    wrong = owned_by_user(appdir)
    check(not wrong, "every file of the prefix is owned by the user (nothing ran as root)", " ".join(wrong[:5]))
    docs = appdir / f"sandbox/prefix/drive_c/users/{USER}/Documents"
    check(docs.is_dir() and not docs.is_symlink(), "the prefix's Documents is private, not linked to the real home")
    check(not any((appdir / "sandbox/installer").iterdir()), "the verified installer copy was removed after installation")
    entry = HOME / f".local/share/applications/boswas-winapp-{APP}.desktop"
    check(entry.is_file() and f"Exec=boswas-winapp launch {APP}" in entry.read_text(), "desktop launcher created")

    p = winapp("--json", "list")
    listed = {a["id"]: a for a in as_json(p).get("applications", [])}
    check(listed.get(APP, {}).get("state") == "installed", "boswas-winapp list shows the application", p.stdout)

    # --- second application: separate prefix, network granted --------------------------------
    p = winapp("--json", "install", str(exe), "--id", NET)
    check(p.returncode == 0, "a second application gets its own prefix", p.stderr)

    # --- launch and isolation probes ----------------------------------------------------------
    probe_args = [
        "read", f"Z:/home/{USER}/.ssh/id_test",
        "read", f"Z:/home/{USER}/Documents/report.txt",
        "read", f"Z:/home/{USER}/.local/share/boswas/wine/{NET}/app.json",
        "list", "Z:/home",
        "write", f"Z:/home/{USER}/pwned.txt",
        "write", "Z:/etc/pwned.txt",
        "write", "Z:/usr/pwned.txt",
        "list", "Z:/var/lib/boswas/wine",
        "read", f"Z:/var/lib/boswas/wine/{NET}/prefix/system.reg",
        "connect", "127.0.0.1", str(LISTEN_PORT),
        "write", f"C:/users/{USER}/AppData/Roaming/probe.txt",
        "read", "Z:/etc/boswas/device.conf",
    ]
    p = winapp("launch", APP, "--", *probe_args)
    check(p.returncode == 0 and "BOSWAS-TESTAPP OK" in p.stdout, "boswas-winapp launch runs the application",
          p.stdout + p.stderr)
    r = probes(p.stdout)
    expectations = [
        (f"read Z:/home/{USER}/.ssh/id_test", "BLOCKED", "the user's SSH keys are not reachable"),
        (f"read Z:/home/{USER}/Documents/report.txt", "BLOCKED", "the user's documents are not reachable (not granted)"),
        (f"read Z:/home/{USER}/.local/share/boswas/wine/{NET}/app.json", "BLOCKED", "other applications' records are not reachable"),
        ("list Z:/home", "BLOCKED", "/home is not visible in the sandbox"),
        (f"write Z:/home/{USER}/pwned.txt", "BLOCKED", "cannot write into the user's home"),
        ("write Z:/etc/pwned.txt", "BLOCKED", "cannot modify /etc"),
        ("write Z:/usr/pwned.txt", "BLOCKED", "cannot modify /usr"),
        (f"read Z:/var/lib/boswas/wine/{NET}/prefix/system.reg", "BLOCKED", "another application's prefix is not visible"),
        (f"connect 127.0.0.1:{LISTEN_PORT}", "BLOCKED", "no network access unless granted (host loopback service unreachable)"),
        (f"write C:/users/{USER}/AppData/Roaming/probe.txt", "ALLOWED", "positive control: the application can write its own prefix"),
    ]
    for key, expected, text in expectations:
        check(r.get(key) == expected, f"isolation: {text}", f"{key} -> {r.get(key)}")
    for leaked in (HOME / "pwned.txt", Path("/etc/pwned.txt"), Path("/usr/pwned.txt")):
        check(not leaked.exists(), f"isolation: {leaked} was not created on the host")
    listing = next((line for line in p.stdout.splitlines() if line.startswith("PROBE list Z:/var/lib/boswas/wine ")), "")
    check(listing.endswith("(3 entries)"), "isolation: only the application's own state is under /var/lib/boswas/wine", listing)
    device_conf = r.get("read Z:/etc/boswas/device.conf")
    if enforcing:
        check(device_conf == "BLOCKED", "AppArmor: files outside the profile (/etc/boswas/device.conf) are denied")
    else:
        emit("INFO", f"read /etc/boswas/device.conf without AppArmor: {device_conf} (denied only by the AppArmor layer)")

    p = winapp("--json", "status", APP)
    last = as_json(p).get("last_launch") or {}
    check(last.get("exit_code") == 0, "status records the last launch", p.stdout)
    if enforcing:
        check(last.get("confinement") == ENFORCED, "the application ran confined by boswas-winapp (enforce)", str(last))
    else:
        emit("INFO", f"confinement reported by the runner: {last.get('confinement')}")
    p = winapp("logs", APP)
    check(p.returncode == 0 and "BOSWAS-TESTAPP OK" in p.stdout, "boswas-winapp logs shows the launch output", p.stderr)

    p = winapp("launch", NET, "--quiet", "--", "connect", "127.0.0.1", str(LISTEN_PORT), "list", "Z:/var/lib/boswas/wine")
    rnet = probes(run(["boswas-winapp", "logs", NET]).stdout)
    check(rnet.get(f"connect 127.0.0.1:{LISTEN_PORT}") == "ALLOWED",
          "network is available when the manifest grants it", str(rnet))
    p = winapp("launch", APP, "--quiet", "--", "exit", "7")
    check(p.returncode == 7, "launch returns the Windows program's exit code", str(p.returncode))

    # --- one instance per prefix -----------------------------------------------------------------
    bg = subprocess.Popen(["boswas-winapp", "launch", APP, "--quiet", "--", "sleep", "8000"], user=UID, group=UID,
                          extra_groups=[], env={"PATH": "/usr/bin:/bin", "HOME": str(HOME), "LANG": "C.UTF-8"},
                          cwd=str(HOME), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    p = winapp("launch", APP, "--quiet")
    check(p.returncode == 6, "a second launch of a running application is refused (exit 6, busy)", str(p.returncode))
    p = winapp("remove", APP)
    check(p.returncode == 6, "a running application cannot be removed (exit 6)", str(p.returncode))
    bg.wait(timeout=300)

    # --- policy refusals -------------------------------------------------------------------------
    blocked = HOME / "Downloads/boswas-testapp-blocked.exe"
    p = winapp("install", str(blocked), "--id", "local.renamed")
    check(p.returncode == 4 and "blocked" in p.stderr, "a blocked installer is refused even under another ID (exit 4)",
          p.stderr)
    check(not (DATA / "local.renamed").exists(), "nothing was installed for the blocked installer")

    unlisted = HOME / "Downloads/boswas-testapp-unlisted.exe"
    p = winapp("--json", "install", str(unlisted), "--id", UNLISTED, "--name", "Unlisted Test", "--", "/S")
    doc = as_json(p).get("application", {})
    check(p.returncode == 0 and doc.get("status") == "unknown" and doc.get("manifest_source") == "unlisted",
          "an unlisted installer is allowed by the shipped policy, with status unknown", p.stdout + p.stderr)
    check(doc.get("launch") == "C:\\Program Files\\Boswas Test App\\boswas-testapp.exe",
          "the unlisted application's program was detected after installation", str(doc.get("launch")))
    p = winapp("--json", "status", UNLISTED)
    check(as_json(p).get("sandbox", {}).get("network") is False, "unlisted applications get no network (policy)", p.stdout)
    with open("/etc/boswas/compat/policy.conf", "a") as fh:
        fh.write('UNLISTED_APPS="deny"\n')
    p = winapp("install", str(unlisted), "--id", "local.second-unlisted")
    check(p.returncode == 4 and "UNLISTED_APPS=deny" in p.stderr, "policy UNLISTED_APPS=deny refuses unlisted installers",
          p.stderr)
    p = winapp("launch", UNLISTED, "--quiet")
    check(p.returncode == 4, "policy is re-checked at launch (installed unlisted application refused)", p.stderr)

    # --- repair and remove -------------------------------------------------------------------------
    shutil.rmtree(appdir / "sandbox/home")
    p = winapp("--json", "repair", APP)
    check(p.returncode == 0 and as_json(p).get("healthy") is True and (appdir / "sandbox/home").is_dir(),
          "repair recreates missing state and updates the prefix", p.stdout + p.stderr)
    p = winapp("launch", APP, "--quiet")
    check(p.returncode == 0, "the application starts after repair", p.stderr)
    (appdir / "sandbox/prefix/drive_c/Program Files/Boswas Test App/boswas-testapp.exe").unlink()
    p = winapp("--json", "repair", APP)
    check(p.returncode == 1 and as_json(p).get("healthy") is False,
          "repair reports a missing program as needing reinstallation (exit 1)", p.stdout)
    p = winapp("remove", APP)
    check(p.returncode == 0 and not appdir.exists() and not entry.exists(),
          "boswas-winapp remove deletes the prefix and the launcher", p.stderr)
    p = winapp("--json", "list")
    check(APP not in {a["id"] for a in as_json(p).get("applications", [])}, "removed application no longer listed")

    # --- inventory for the device agent ------------------------------------------------------------
    p = winapp("--json", "list", "--all-users", user=False)
    users = as_json(p).get("users", [])
    apps = {a["id"] for u in users for a in u["applications"]}
    check(p.returncode == 0 and NET in apps and UNLISTED in apps,
          "root inventory (list --all-users) reports every user's applications", p.stdout + p.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # report, keep the suite's output parseable
        emit("FAIL", f"scenario aborted: {type(exc).__name__}: {exc}")
        sys.exit(1)
