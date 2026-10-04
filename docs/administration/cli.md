# Boswas command-line tools

Installed by `boswas-cli`. All commands are **read-only** in v1 alpha,
work offline, and never print credentials, tokens or key material.

```
boswas [--json] <command> [<action>]
boswas-info   [--json]     # = boswas info
boswas-status [--json]     # = boswas status
```

`--json` may be placed before or after the command.

## Commands

| Command | Shows |
|---------|-------|
| `boswas info` | OS name, version, build ID, channel, Debian base and codename, architecture, kernel, desktop, hostname, boot mode and Secure Boot state, live or installed, device ID, enrollment, compliance summary |
| `boswas status` | All posture checks with PASS/WARN/FAIL/INFO/UNKNOWN |
| `boswas security status` | Security checks only |
| `boswas device status` | `/etc/boswas/device.conf` identifiers plus a non-sensitive hardware summary (no serial numbers) |
| `boswas policy status` | Policy state (unmanaged until the Phase 4 agent) |
| `boswas update status` | Update channel, repository, automatic security updates, package list age |
| `boswas version` | CLI version |
| `boswas app list` | Reserved for the Boswas Store (Phase 5). Exits 69 |
| `boswas winapp list` | Reserved for WinCompat (Phase 6). Exits 69 |

### Example

Captured by the automated boot test, in the live session of build
`BOS-1.0~alpha1-20261004T004726Z` (a QEMU VM booted via BIOS, without a TPM):

```
$ boswas-info
Boswas OS
Version:      v1 Alpha (1.0~alpha1)
Build:        BOS-1.0~alpha1-20261004T004726Z
Channel:      dev
Base:         Debian 13.7
Codename:     trixie
Architecture: amd64
Kernel:       6.12.111+deb13-amd64
Desktop:      KDE Plasma 6.3.6
Hostname:     boswas-device
Boot:         BIOS
Session:      live (not installed)
Device ID:    not assigned
Enrollment:   unenrolled
Compliance:   COMPLIANT_WITH_WARNINGS (5 pass, 3 warn, 0 fail, 0 unknown)

$ boswas-status
  CHECK               STATUS   DETAIL
  Secure Boot         WARN     legacy BIOS boot; Boswas OS is UEFI-first
  TPM                 WARN     no TPM detected
  Disk encryption     INFO     live session: read-only image with RAM overlay, nothing is persisted
  Firewall            PASS     nftables.service active (Boswas baseline ruleset)
  AppArmor            WARN     enabled in kernel; profiles not loaded in the live session (Debian skips them on live media)
  Audit logging       PASS     auditd.service active (Boswas audit rules)
  SSH server          PASS     not installed (no inbound remote access)
  Root account        UNKNOWN  run as root to check
  Repository trust    INFO     live session: only the boot medium's package pool is unsigned (removed at install)
  Screen lock         PASS     automatic lock after 10 min idle, enforced
  USB storage policy  INFO     mode=allow (framework only; enforcement not implemented in v1 alpha)
  Security updates    PASS     automatic security updates enabled; package lists 0 day(s) old
  Boswas agent        INFO     not installed in v1 alpha (device agent arrives in Phase 4)
  Enrollment          INFO     unenrolled
```

On an installed device booted with UEFI and Secure Boot on TPM 2.0
hardware, Secure Boot, TPM, AppArmor, disk encryption and repository trust
are all expected to report PASS.

Some checks need root. For example, `root-account` reads `/etc/shadow`:
run `sudo boswas status` for the complete picture. Without root, those
checks report UNKNOWN and are not scored.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success. For status commands, no check failed |
| 1 | At least one posture check FAILed (`NON_COMPLIANT`) |
| 2 | Usage error |
| 69 | Command planned for a later phase |
| 70 | Internal error |

## JSON

Every document carries `"schema": "boswas-cli/1"`, `"command"` and
`"generated_at"` (UTC). Example (`boswas --json status`, shortened):

```json
{
  "schema": "boswas-cli/1",
  "command": "status",
  "generated_at": "2026-10-04T08:00:00Z",
  "compliance": {"state": "COMPLIANT_WITH_WARNINGS", "pass": 8, "warn": 1, "fail": 0, "unknown": 0,
                 "basis": "local self-assessment against the Boswas OS v1 alpha baseline (not attested)"},
  "checks": [
    {"id": "firewall", "category": "security", "title": "Firewall", "status": "PASS",
     "detail": "nftables.service active (Boswas baseline ruleset)", "scored": true}
  ]
}
```

Check IDs are stable:

| Category | Check IDs |
|----------|-----------|
| Security | `secure-boot`, `tpm`, `disk-encryption`, `firewall`, `apparmor`, `audit`, `ssh-server`, `root-account`, `apt-trust`, `screen-lock`, `usb-policy` |
| Updates | `updates` |
| Management | `agent`, `enrollment` |

## Logging

The v1 alpha CLI does not write logs. The journal identifiers `boswas-device`,
`boswas-security`, `boswas-update` and `boswas-winapp` are reserved for the
services that arrive in Phases 4–7.
