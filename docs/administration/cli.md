# Boswas command-line tools

`boswas`, `boswas-info` and `boswas-status` are installed by `boswas-cli`;
`boswas-winapp` by `boswas-compat`; `boswas-device` by `boswas-device-agent`
(see below). The Control Plane's `boswas-cp` runs on the server
([control-plane.md](../device-management/control-plane.md)). The `boswas` commands are
**read-only**,
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
| `boswas device status` | Device ID, enrollment, Control Plane and policy version (from the device agent; `/etc/boswas/device.conf` as fallback), profile and certificate reference, the agent's device state and connection, and a non-sensitive hardware summary (no serial numbers) |
| `boswas policy status` | Policy state: the version of the signed Control Plane policy applied by the device agent, or unmanaged |
| `boswas update status` | Update channel, repository, automatic security updates, package list age |
| `boswas version` | CLI version |
| `boswas app list` | Reserved for the Boswas Store (Milestone 6). Exits 69 |
| `boswas winapp ...` | Runs `boswas-winapp ...` (passes `--json`). Exits 69 if `boswas-compat` is not installed |

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
| 69 | Command not available: planned for a later milestone, or its package is not installed |
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
| Security | `secure-boot`, `tpm`, `disk-encryption`, `firewall`, `apparmor`, `audit`, `ssh-server`, `root-account`, `apt-trust`, `screen-lock`, `usb-policy`, `winapp-confinement` |
| Updates | `updates` |
| Management | `agent`, `enrollment` |

### `winapp-confinement`

`winapp-confinement` reports whether the `boswas-winapp` AppArmor profile is
loaded in enforce mode. It reads `/sys/kernel/security/apparmor/profiles`,
which only root can read:

| Situation | Result |
|-----------|--------|
| `boswas-compat` not installed | INFO, unscored |
| Run without root | UNKNOWN, unscored |
| Profile loaded in enforce mode | PASS |
| Profile in complain mode, or not loaded on an installed system | WARN. Windows applications refuse to start in that case |
| Live session | INFO: Debian does not load AppArmor profiles on live media |

## boswas-winapp (WinCompat)

Installed by `boswas-compat`. Windows applications in isolated, confined Wine
sandboxes; the design is in
[docs/compatibility/README.md](../compatibility/README.md).

```
boswas-winapp [--json] inspect INSTALLER [--id ID] [--name NAME] [--portable]
boswas-winapp [--json] install INSTALLER [--id ID] [--name NAME] [--portable] [--interactive]
                                         [--timeout S] [--verbose] [-- INSTALLER-ARGS]
boswas-winapp [--json] upgrade APPLICATION INSTALLER [--interactive] [--timeout S] [--verbose] [-- ARGS]
boswas-winapp [--json] remove APPLICATION
boswas-winapp [--json] list [--all-users]
boswas-winapp [--json] launch APPLICATION [--exe PROGRAM] [--timeout S] [--quiet] [-- ARGS]
boswas-winapp [--json] stop APPLICATION
boswas-winapp [--json] status APPLICATION
boswas-winapp [--json] repair APPLICATION [--timeout S]
boswas-winapp [--json] logs APPLICATION [--install | --repair | --launch | --clear] [--lines N]
boswas-winapp [--json] catalog
boswas-winapp [--json] runtime
boswas-winapp [--json] manifest validate FILE...
```

| Command | Does |
|---------|------|
| `inspect` | What `install` would decide (type, architecture, catalog match, sandbox, policy) without creating or running anything. Exit 0: accepted, 4: refused |
| `install` | Verifies the installer, matches the catalog (SHA-256 or `--id`), applies policy, creates the prefix, runs the installer in the sandbox, creates a desktop launcher |
| `upgrade` | Runs a newer installer in the existing prefix (catalogued applications: only the pinned installer) |
| `remove` | Deletes the application, its prefix and its launcher (refused while it runs) |
| `list` | The user's applications with their effective status and normalised state (`app_state`). `--all-users` (root only) is the device inventory: IDs, versions, statuses, architectures and running applications |
| `launch` | Starts the application in its sandbox; output is shown and logged |
| `stop` | Ends a running application's sandbox and all its Windows processes (a launch ended this way exits 0 and reports `"stopped": true`) |
| `runtime` | Wine, architectures (x86_64 only), bubblewrap, AppArmor profile mode, policy in effect; exit 1 if unhealthy |
| `status` | State, catalog status, policy decision, sandbox grants, last launch, AppArmor confinement |
| `repair` | Recreates missing state, updates the prefix, re-applies Boswas defaults and the launcher, checks the program still exists |
| `logs` | The latest launch, install or repair log; `--clear` deletes the application's logs |
| `catalog` | Effective manifests per layer, ignored manifests, effective policy |
| `manifest validate` | Validates manifest files (administrators, CI) |

**Root.** `install`, `upgrade`, `launch`, `stop`, `repair` and `remove`
refuse to run as root (exit 4). Windows software always runs as the user who
uses it.

**32-bit.** 32-bit installers are refused (exit 4, reason `architecture`)
with "This application requires 32-bit Windows compatibility, which is not
supported by Boswas OS." Boswas OS v1 runs 64-bit Windows applications only.

**JSON.** Every document carries `"schema": "boswas-winapp/1"`, `"command"`
and `"generated_at"`. Errors in JSON mode carry
`{"error": {"reason": ..., "message": ...}}`. Reasons include `root`,
`blocked`, `unlisted-denied`, `status-not-allowed`, `installer-mismatch`,
`architecture`, `dependencies`, `winetricks`, `already-installed`,
`policy-blocked`, `policy-not-allowed`, `up-to-date`, `stop-unverified`,
`confinement`, `sandbox` and `not-found`.

**Exit codes.**

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | The operation ran and failed (installer error, prefix creation failed, repair found a missing program) |
| 2 | Usage error (including an installer pinned by several manifests without `--id`) |
| 3 | Application, installer or manifest not found |
| 4 | Refused by policy or a safety rule: root, blocked, unlisted denied, status not allowed, installer mismatch, 32-bit program |
| 5 | Runtime unavailable: Wine or bubblewrap missing, or no enforcing AppArmor confinement |
| 6 | Busy: the application is running or another operation holds it |
| 70 | Internal error |

For `launch`, once the program has started the exit code is the Windows
program's own (values above 255 become 1).

### Example

Captured in the live VM during development: the Windows test application,
installed under a local catalog manifest that grants network access.

```
$ boswas-winapp install /mnt/fixtures/boswas-testapp.exe --id com.boswas.testapp-net
boswas-winapp: verifying boswas-testapp.exe
boswas-winapp: creating the Wine prefix (first run takes a while)
boswas-winapp: running the installer in its sandbox (/S)
boswas-winapp: installed com.boswas.testapp-net; start it with: boswas-winapp launch com.boswas.testapp-net
Application: Boswas Test App (network) (com.boswas.testapp-net)
Version:     1.0
Status:      experimental (local)
Program:     C:\Program Files\Boswas Test App\boswas-testapp.exe
Sandbox:     network
Log:         /home/boswas/.local/share/boswas/wine/com.boswas.testapp-net/logs/install-20261004T035143Z.log

$ boswas-winapp launch com.boswas.testapp-net -- connect 127.0.0.1 47011
BOSWAS-TESTAPP OK (variant 1)
PROBE connect 127.0.0.1:47011 ALLOWED
```

(The progress lines go to stderr.)

## boswas-device (device agent)

Installed by `boswas-device-agent`. Details:
[docs/device-management/README.md](../device-management/README.md).

```
boswas-device [--json] status
boswas-device [--json] identity [reset --yes]
boswas-device [--json] config validate [--file FILE] | config show
boswas-device [--json] inventory [--refresh]
boswas-device [--json] policy | commands [--limit N] | events [--limit N]
boswas-device [--json] enroll [--token-file FILE]      # token from the file or standard input, never argv
boswas-device [--json] unenroll --yes | sync | maintenance on|off
```

| Command | Shows or does |
|---------|---------------|
| `status` | Device state, agent version, Control Plane connection, last contact and error, applied policy, pending messages. Works without the service (last published status) |
| `identity` | Device ID, creation, source (generated or provisioned), ephemeral or persistent, certificate fingerprint |
| `config validate` | Checks `/etc/boswas/device.conf` (or `--file`); exit 1 on errors |
| `inventory` | Exactly what would be reported; `--refresh` collects now (root) |
| `policy` | Applied signed policy and the effective WinCompat policy |
| `commands`, `events` | Remote commands and their outcomes; the device event log |
| `enroll`, `unenroll`, `sync`, `maintenance`, `identity reset` | Root only |

Exit codes: 0 success; 1 failed or invalid configuration; 2 usage; 4 refused
(root required or not permitted); 5 the agent service is not running; 70
internal error. JSON documents carry `"schema": "boswas-device/1"`.

## Logging

The `boswas` CLI does not write logs. `boswas-winapp` keeps per-application
logs in `~/.local/share/boswas/wine/<id>/logs/`, with the last 10 of each
kind kept and each capped at 8 MiB. The device agent logs to the journal
(`journalctl -u boswas-device-agent`), and its event log is
`/var/lib/boswas/agent/events.jsonl`. The session agent logs to the user's
journal (`journalctl --user -u boswas-session-agent`). The journal
identifiers `boswas-security` and `boswas-update` are reserved for later
services.
