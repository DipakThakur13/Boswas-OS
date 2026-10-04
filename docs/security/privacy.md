# Privacy: what administrators can and cannot see

Boswas OS is a company-managed system. Employees are entitled to know exactly
what is collected, who can see it, and what is never collected.

## 1.0~alpha2: nothing leaves the device

There is no device agent service, telemetry, crash reporting or remote
management yet. Nothing is sent to Boswas Group. The device agent's message
formats are defined (Milestone 1) so that their content is reviewable before
anything is ever sent; see "Device agent messages" below. Debian's popularity-contest is
disabled at install. The only outbound connections the OS itself makes are:

- APT and unattended-upgrades to the Debian archive
- NTP time synchronisation
- NetworkManager connectivity checks, if enabled by Debian defaults

## Data categories

| Category | Examples | Collected locally | Sent centrally | Who may see it |
|----------|----------|-------------------|----------------|----------------|
| Device management | OS build, device ID, enrollment state, hardware model, agent version | `boswas device status` (on demand) | Phase 4+, documented before release | IT/Platform admins via the Admin Console |
| Security posture | Secure Boot, TPM, encryption, firewall, AppArmor and audit state | `boswas status` (on demand) | Phase 4+ | IT/Security admins |
| Security events | Changes to identity files, sudoers, security config, kernel modules, clock | auditd log (root-only), max retention set by the journal policy | Phase 4+, policy-controlled forwarding | Security admins, access-controlled and audited |
| Application inventory | Installed packages and Windows apps | dpkg database; per-user WinCompat records (`~/.local/share/boswas/wine/<id>/app.json`) | Milestone 2+: application IDs, versions, statuses and installation **counts**, never user names | IT admins |
| User content | Documents, browser history, e-mail, files in /home | Never by Boswas OS | **Never** | Only the user (and company-approved backup, once defined) |
| Private user activity | Keystrokes, screen contents, command lines, file reads | **Never** | **Never** | Nobody |

## Windows applications (WinCompat)

- **Local data only.** `boswas-winapp` keeps, per user and application, an
  installation record and logs of what the Windows program printed. Both
  stay in the user's home and are never sent anywhere.
- **What root sees.** Root's inventory listing (`--all-users`) reads only
  IDs, versions, statuses and states.
- **What a Windows application sees.** Only its own prefix and the folders
  its manifest grants; never the rest of the user's home.

## Device agent messages (defined, not yet sent)

The heartbeat, enrollment request and compliance report have closed field
lists (`packages/boswas-device-agent/boswas_agent/privacy.py`, schemas in
`packages/boswas-device-agent/schemas/`):

- **Heartbeat:** device ID, agent and OS versions, kernel, uptime,
  compliance counts, policy version, update channel and state, and
  posture-check statuses.
- **Compliance report:** check IDs and statuses only.
- **Enrollment:** adds a certificate request, hardware model facts (no serial
  numbers) and a one-time token.

A test fails if any other field is added. Changing these lists requires
updating this page first.

## Explicit non-goals

Boswas OS does **not** and will not implement:

- keystroke logging
- screen capture or screen recording
- silent inspection of personal files or browsing
- logging of the commands users type (the audit rules exclude `execve` on purpose)

## Rules for any future monitoring

Any future collection must be:

1. documented in this file **before** it ships;
2. controlled by Boswas policy (visible in `boswas policy status`);
3. limited to the stated purpose and minimal fields, with defined retention;
4. readable only by roles with a need, with every administrator access
   itself recorded in the audit trail.

## Local logs

- **Journal:** persistent, capped at 1 GB and 90 days.
- **Audit log:** `/var/log/audit`, root-only, rotated by auditd.
- **Firewall:** dropped inbound connection attempts are logged rate-limited,
  as packet headers only (no payload).
