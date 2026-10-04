"""Local posture checks against the Boswas OS v1 alpha security baseline.

These are a *local self-assessment*: they read kernel, firmware and service
state on this machine. They are not remote attestation and must not be
presented as such. The baseline and the alpha enforcement level of each check
are documented in docs/security/baseline.md.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass

from . import system

PASS, WARN, FAIL, INFO, UNKNOWN = "PASS", "WARN", "FAIL", "INFO", "UNKNOWN"

SECURE_BOOT_VAR = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
KDE_SETTINGS = "/usr/share/boswas/kde-settings"
STALE_PACKAGE_LISTS_DAYS = 7


@dataclass
class Check:
    id: str
    category: str
    title: str
    status: str
    detail: str
    # Unscored checks are informational and never change the compliance state.
    scored: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def secure_boot() -> Check:
    title = "Secure Boot"
    if system.boot_mode() != "UEFI":
        return Check("secure-boot", "security", title, WARN, "legacy BIOS boot; Boswas OS is UEFI-first")
    data = system.read_bytes(SECURE_BOOT_VAR)
    # efivarfs: 4 bytes of attributes followed by the 1-byte value.
    if not data or len(data) < 5:
        return Check("secure-boot", "security", title, UNKNOWN, "SecureBoot EFI variable not readable")
    if data[4] == 1:
        return Check("secure-boot", "security", title, PASS, "enabled")
    return Check("secure-boot", "security", title, WARN, "disabled in firmware (required for production)")


def tpm() -> Check:
    major = system.read_text("/sys/class/tpm/tpm0/tpm_version_major")
    if major == "2":
        return Check("tpm", "security", "TPM", PASS, "TPM 2.0 present")
    if major:
        return Check("tpm", "security", "TPM", WARN, f"TPM {major}.x present; TPM 2.0 required")
    return Check("tpm", "security", "TPM", WARN, "no TPM detected")


def _root_device_dir() -> str | None:
    """sysfs directory of the block device backing '/' (via its major:minor)."""
    mountinfo = system.read_text("/proc/self/mountinfo") or ""
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 4 and fields[4] == "/":
            return f"/sys/dev/block/{fields[2]}"
    return None


def _find_crypt(devdir: str, depth: int = 0) -> str | None:
    """Walk device-mapper stacking (e.g. LVM on LUKS) looking for a crypt target."""
    if depth > 8:
        return None
    uuid = system.read_text(f"{devdir}/dm/uuid") or ""
    if uuid.startswith("CRYPT-LUKS2"):
        return "LUKS2"
    if uuid.startswith("CRYPT-LUKS1"):
        return "LUKS1"
    if uuid.startswith("CRYPT-"):
        return "dm-crypt (non-LUKS)"
    slaves = system.sysroot_path(f"{devdir}/slaves")
    try:
        names = sorted(os.listdir(slaves))
    except OSError:
        return None
    for name in names:
        found = _find_crypt(f"{devdir}/slaves/{name}", depth + 1)
        if found:
            return found
    return None


def disk_encryption() -> Check:
    title = "Disk encryption"
    if system.is_live_session():
        return Check("disk-encryption", "security", title, INFO,
                     "live session: read-only image with RAM overlay, nothing is persisted", scored=False)
    devdir = _root_device_dir()
    if devdir is None:
        return Check("disk-encryption", "security", title, UNKNOWN, "cannot determine root device")
    kind = _find_crypt(devdir)
    if kind == "LUKS2":
        return Check("disk-encryption", "security", title, PASS, "root filesystem on LUKS2")
    if kind:
        return Check("disk-encryption", "security", title, WARN, f"root filesystem on {kind}; LUKS2 required")
    return Check("disk-encryption", "security", title, FAIL, "root filesystem is not encrypted")


def _service_check(check_id: str, title: str, unit: str, purpose: str) -> Check:
    active = system.unit_active(unit)
    if active == "active":
        return Check(check_id, "security", title, PASS, f"{unit} active ({purpose})")
    if active == "unknown":
        enabled = system.unit_enabled(unit)
        return Check(check_id, "security", title, UNKNOWN, f"systemd not running; {unit} is {enabled}")
    return Check(check_id, "security", title, FAIL, f"{unit} is {active}")


def firewall() -> Check:
    return _service_check("firewall", "Firewall", "nftables.service", "Boswas baseline ruleset")


def audit() -> Check:
    return _service_check("audit", "Audit logging", "auditd.service", "Boswas audit rules")


def apparmor() -> Check:
    title = "AppArmor"
    enabled = system.read_text("/sys/module/apparmor/parameters/enabled")
    if enabled is None:
        return Check("apparmor", "security", title, FAIL, "AppArmor not available in this kernel")
    if enabled != "Y":
        return Check("apparmor", "security", title, FAIL, "disabled in the kernel")
    # The kernel flag alone is not enough: profiles are loaded by apparmor.service.
    state = system.unit_active("apparmor.service")
    if state == "active":
        return Check("apparmor", "security", title, PASS, "enabled, profiles loaded")
    if state == "unknown":
        return Check("apparmor", "security", title, UNKNOWN, "enabled in kernel; systemd not running")
    if system.is_live_session():
        # Debian's apparmor.service is skipped on live media
        # (ConditionPathExists=!/run/live/overlay/work); installed systems load profiles.
        return Check("apparmor", "security", title, WARN,
                     "enabled in kernel; profiles not loaded in the live session (Debian skips them on live media)")
    return Check("apparmor", "security", title, FAIL, f"enabled in kernel but profiles not loaded (apparmor.service {state})")


APPARMOR_PROFILES = "/sys/kernel/security/apparmor/profiles"
WINAPP_PROFILE_FILE = "/etc/apparmor.d/boswas-winapp"
WINAPP_PROFILE = "boswas-winapp"


def winapp_confinement() -> Check:
    """The AppArmor profile that confines Windows applications (boswas-compat)."""
    cid, title = "winapp-confinement", "Windows app confinement"
    if not system.sysroot_path(WINAPP_PROFILE_FILE).exists():
        return Check(cid, "security", title, INFO, "WinCompat (boswas-compat) not installed", scored=False)
    if not system.is_root():
        return Check(cid, "security", title, UNKNOWN, "run as root to check", scored=False)
    profiles = system.read_text(APPARMOR_PROFILES)
    for line in (profiles or "").splitlines():
        name, _, mode = line.strip().partition(" ")
        if name == WINAPP_PROFILE:
            mode = mode.strip("()")
            if mode == "enforce":
                return Check(cid, "security", title, PASS, "boswas-winapp AppArmor profile enforcing")
            return Check(cid, "security", title, WARN, f"boswas-winapp profile loaded in {mode} mode (not enforcing)")
    if system.is_live_session():
        return Check(cid, "security", title, INFO,
                     "live session: AppArmor profiles are not loaded on live media", scored=False)
    if profiles is None:
        return Check(cid, "security", title, WARN, "AppArmor not available; Windows applications will not start")
    return Check(cid, "security", title, WARN,
                 "boswas-winapp profile not loaded; Windows applications will not start")


def ssh_server() -> Check:
    title = "SSH server"
    if not system.sysroot_path("/usr/sbin/sshd").exists():
        return Check("ssh-server", "security", title, PASS, "not installed (no inbound remote access)")
    if system.unit_active("ssh.service") == "active":
        return Check("ssh-server", "security", title, WARN, "running; must be explicitly provisioned")
    return Check("ssh-server", "security", title, PASS, "installed but not running")


def root_account() -> Check:
    title = "Root account"
    if not system.is_root():
        return Check("root-account", "security", title, UNKNOWN, "run as root to check", scored=False)
    shadow = system.read_text("/etc/shadow") or ""
    for line in shadow.splitlines():
        fields = line.split(":")
        if len(fields) > 1 and fields[0] == "root":
            # Only the lock marker is inspected; the hash itself is never reported.
            password = fields[1]
            if password == "":
                return Check("root-account", "security", title, FAIL, "root has an empty password")
            if password.startswith(("!", "*")):
                return Check("root-account", "security", title, PASS, "password login locked")
            return Check("root-account", "security", title, WARN, "root has a usable password")
    return Check("root-account", "security", title, UNKNOWN, "root entry not found")


def _kde_key(filename: str, key: str) -> str | None:
    text = system.read_text(f"{KDE_SETTINGS}/{filename}") or ""
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        # KConfig keys may carry flags, e.g. "Autolock[$i]".
        if sep and name.split("[", 1)[0].strip() == key:
            return value.strip()
    return None


def screen_lock() -> Check:
    autolock = _kde_key("kscreenlockerrc", "Autolock")
    timeout = _kde_key("kscreenlockerrc", "Timeout")
    if autolock == "true":
        return Check("screen-lock", "security", "Screen lock", PASS,
                     f"automatic lock after {timeout or '?'} min idle, enforced")
    return Check("screen-lock", "security", "Screen lock", WARN, "Boswas screen lock policy not installed")


LIVE_MEDIUM_SOURCE = "file:/run/live/medium"


def _apt_source_files() -> list[str]:
    files = ["/etc/apt/sources.list"]
    try:
        for name in sorted(os.listdir(system.sysroot_path("/etc/apt/sources.list.d"))):
            if name.endswith((".list", ".sources")):
                files.append(f"/etc/apt/sources.list.d/{name}")
    except OSError:
        pass
    return files


def apt_trust() -> Check:
    """Every enabled APT source must be signature-verified (no trusted=yes)."""
    title = "Repository trust"
    unauthenticated = []
    for path in _apt_source_files():
        for line in (system.read_text(path) or "").splitlines():
            entry = line.split("#", 1)[0].strip()
            lowered = entry.lower().replace(" ", "")
            if "trusted=yes" in lowered or lowered == "trusted:yes":
                unauthenticated.append(entry)
    if not unauthenticated:
        return Check("apt-trust", "security", title, PASS, "all APT sources are signature-verified")
    if system.is_live_session() and all(LIVE_MEDIUM_SOURCE in e for e in unauthenticated):
        return Check("apt-trust", "security", title, INFO,
                     "live session: only the boot medium's package pool is unsigned (removed at install)", scored=False)
    return Check("apt-trust", "security", title, FAIL,
                 f"{len(unauthenticated)} APT source(s) bypass signature verification (trusted=yes)")


def usb_policy() -> Check:
    conf = system.load_env("/etc/boswas/usb-policy/policy.conf")
    mode = conf.get("MODE", "unset")
    return Check("usb-policy", "security", "USB storage policy", INFO,
                 f"mode={mode} (framework only; enforcement not implemented in v1 alpha)", scored=False)


def unattended_upgrades_enabled() -> bool:
    code, out = system.run(["apt-config", "shell", "UU", "APT::Periodic::Unattended-Upgrade"])
    return code == 0 and out.replace("'", "").endswith("=1")


def updates() -> Check:
    title = "Security updates"
    enabled = unattended_upgrades_enabled()
    age_days = package_lists_age_days()
    age = "unknown age" if age_days is None else f"{age_days:.0f} day(s) old"
    if not enabled:
        return Check("updates", "updates", title, WARN, f"automatic security updates disabled; package lists {age}")
    if age_days is not None and age_days > STALE_PACKAGE_LISTS_DAYS:
        return Check("updates", "updates", title, WARN, f"automatic security updates enabled; package lists {age}")
    return Check("updates", "updates", title, PASS, f"automatic security updates enabled; package lists {age}")


def package_lists_age_days() -> float | None:
    for candidate in ("/var/lib/apt/periodic/update-success-stamp", "/var/lib/apt/lists"):
        try:
            mtime = system.sysroot_path(candidate).stat().st_mtime
        except OSError:
            continue
        return max(0.0, (time.time() - mtime) / 86400)
    return None


def _agent_status() -> dict:
    text = system.read_text("/var/lib/boswas/agent/status.json")
    try:
        doc = json.loads(text) if text else {}
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def agent() -> Check:
    if system.sysroot_path("/usr/lib/systemd/system/boswas-device-agent.service").exists():
        detail = f"boswas-device-agent.service {system.unit_active('boswas-device-agent.service')}"
        status = _agent_status()
        if status.get("state"):
            detail += f"; device {status['state']}, Control Plane {status.get('connection', 'unknown')}"
        return Check("agent", "management", "Boswas agent", INFO, detail, scored=False)
    return Check("agent", "management", "Boswas agent", INFO, "boswas-device-agent not installed", scored=False)


def enrollment() -> Check:
    state = _agent_status().get("enrollment")
    if not isinstance(state, str):
        state = system.load_env("/etc/boswas/device.conf").get("ENROLLMENT_STATE") or "unenrolled"
    return Check("enrollment", "management", "Enrollment", INFO, state, scored=False)


SECURITY_CHECKS = (secure_boot, tpm, disk_encryption, firewall, apparmor, audit,
                   ssh_server, root_account, apt_trust, screen_lock, usb_policy, winapp_confinement)
ALL_CHECKS = SECURITY_CHECKS + (updates, agent, enrollment)


def run_checks(checks=ALL_CHECKS) -> list[Check]:
    results = []
    for fn in checks:
        try:
            results.append(fn())
        except Exception as exc:  # a broken probe must never hide the others
            results.append(Check(fn.__name__, "internal", fn.__name__, UNKNOWN, f"check failed: {exc}"))
    return results


def compliance(results: list[Check]) -> dict:
    scored = [c for c in results if c.scored]
    counts = {s: sum(1 for c in scored if c.status == s) for s in (PASS, WARN, FAIL, UNKNOWN)}
    if counts[FAIL]:
        state = "NON_COMPLIANT"
    elif counts[WARN] or counts[UNKNOWN]:
        state = "COMPLIANT_WITH_WARNINGS"
    else:
        state = "COMPLIANT"
    return {
        "state": state,
        "pass": counts[PASS],
        "warn": counts[WARN],
        "fail": counts[FAIL],
        "unknown": counts[UNKNOWN],
        "basis": "local self-assessment against the Boswas OS v1 alpha baseline (not attested)",
    }
