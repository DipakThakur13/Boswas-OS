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

```
$ boswas-info
Boswas OS
Version:       v1 Alpha (1.0~alpha1)
Build:         BOS-1.0~alpha1-20261004T...Z
Channel:       dev
Base:          Debian 13.x
Codename:      trixie
Architecture:  amd64
Kernel:        6.12.x-amd64
Desktop:       KDE Plasma 6.3.x
Hostname:      boswas-device
Boot:          UEFI (Secure Boot: enabled)
Session:       installed
Device ID:     not assigned
Enrollment:    unenrolled
Compliance:    COMPLIANT_WITH_WARNINGS (8 pass, 1 warn, 0 fail, 0 unknown)
```

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
